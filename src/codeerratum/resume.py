"""Resume a run that stopped early or hit a budget, without re-paying.

A run is resumable because its artifacts already form a complete ledger:

- ``units.json``     the full unit queue, in split order
- ``calls.json``     every *successful* review call (a unit that failed with
  provider_error never gets an entry), so the intersection of called unit_ids
  across reviewer models is exactly the set of units the last report covers
- ``findings.json``  the full collection dump: mode, reviewer/falsifier seats,
  findings, refuted findings, rejected entries, comparison

Resume therefore needs no extra checkpoint file: it loads the ledger, computes
the units the report does not cover yet, runs those through the normal
orchestrator with the same seats (a subset registry pins the reviewer and
falsifier models so re-routing cannot silently swap them), and merges the two
collections. Same ``run_id``, updated artifacts — one run, several sittings,
which is how a 5-hour token window survives a 1128-unit diff.

Units that were only *partially* reviewed (model A finished them, model B
crashed) are re-run from scratch by both reviewers: merging half-reviewed
units across sittings would corrupt the clustering input, and the wasted
model-A calls are the cheaper side of that trade.
"""

import asyncio
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from .config_store import effective as effective_config
from .i18n import get_lang, t
from .ingest import get_commit, new_side_sources
from .models import FindingCollection, ModelRegistry
from .models.finding import is_unit_failure
from .models.unit import ReviewUnit
from .pipeline import (
    ConfigError,
    ReviewOutcome,
    atomic_write_text,
    prioritize,
    resolve_house_rules,
    resolve_mode,
    store_for,
    token_budget_from_env,
)
from .report import render_markdown
from .review.aggregator import rank_findings
from .review.orchestrator import ReviewCancelled, _zone_split, run_review

STATE_DIR = ".codeerratum"


def _run_dir(repo_root: Path, run_id: str) -> Path:
    run_dir = repo_root / STATE_DIR / "runs" / run_id
    if not run_dir.is_dir():
        raise LookupError(t("resume.no_such_run", run_id=run_id))
    return run_dir


def _load_json(run_dir: Path, name: str):
    path = run_dir / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        # A half-written ledger (crash, disk full) must fail with a sentence
        # naming the file, not a bare traceback the user cannot act on.
        raise LookupError(t("resume.corrupt_ledger", name=name, error=exc)) from exc


# A resume is one writer per run. Two sittings merging concurrently each read
# the old ledger and overwrite the merged one — the later writer silently
# discards the earlier batch's calls and findings. The lock file is crash-safe:
# it carries the owner pid and mtime, so a stale lock from a dead process is
# stolen quickly instead of blocking forever. A lock whose owner is still alive
# is NEVER stolen on age alone: a long batch (1128 units / multi-hour token
# window) would otherwise be entered twice and its paid results overwritten.
RESUME_LOCK_STALE_S = 3600
RESUME_LOCK_DEAD_GRACE_S = 2.0


def _owner_alive(pid: int) -> bool:
    """Check the recorded pid before treating a lock as abandoned.

    Windows has no portable "signal 0" probe; there the mtime fallback below
    is the only option and a dead owner's lock clears after the stale window.
    """
    if os.name == "nt":
        return True
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # another user's live process
    except OSError:
        return False
    return True


def _run_lock(run_dir: Path) -> Path:
    lock = run_dir / "resume.lock"
    deadline = time.monotonic() + 15.0
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return lock
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
                try:
                    owner = int(lock.read_text(encoding="ascii").strip())
                except (OSError, ValueError):
                    owner = -1
                if owner <= 0 or os.name == "nt":
                    abandoned = age > RESUME_LOCK_STALE_S
                else:
                    abandoned = age > RESUME_LOCK_DEAD_GRACE_S and not _owner_alive(owner)
                if abandoned:
                    lock.unlink(missing_ok=True)
                    continue
            except OSError:
                pass  # vanished between stat and unlink; retry the create
            if time.monotonic() >= deadline:
                raise ConfigError(t("resume.locked", path=lock)) from None
            time.sleep(0.25)


def _release_run_lock(lock: Path | None) -> None:
    if lock is not None:
        try:
            lock.unlink(missing_ok=True)
        except OSError:
            pass


def _review_done_by_model(calls: list[dict], reviewer_models: list[str]) -> dict[str, set[str]]:
    """Successful review calls per reviewer model.

    Review calls carry no ``role`` key; verify/falsify calls do. A unit whose
    review crashed (provider_error) never produced a call entry, so "has a
    call entry" is exactly "this model finished reviewing this unit".
    """
    done: dict[str, set[str]] = {}
    for c in calls or []:
        if c.get("role"):
            continue
        model = c.get("model")
        if model in reviewer_models and c.get("unit_id"):
            done.setdefault(model, set()).add(c["unit_id"])
    return done


def plan_resume(
    repo_root: Path,
    run_id: str,
    more: int | None = None,
    paths: str = "",
) -> dict:
    """Compute what a resume would run without spending anything.

    Returns the plan dict the UI shows before the user commits tokens: how
    many units the old report already covers, how many are left in the queue,
    and which slice the next batch would take. ``paths`` narrows the batch by
    file prefix; ``more`` caps its size.
    """
    repo_root = repo_root.resolve()
    run_dir = _run_dir(repo_root, run_id)
    old = _load_json(run_dir, "findings.json")
    if not old:
        raise LookupError(t("resume.no_report", run_id=run_id))
    reviewer_models: list[str] = old.get("reviewer_models") or []
    if not reviewer_models:
        raise ConfigError(t("resume.no_seats"))
    calls = _load_json(run_dir, "calls.json") or []
    done_by_model = _review_done_by_model(calls, reviewer_models)
    fully_done = set.intersection(*[done_by_model.get(m, set()) for m in reviewer_models])
    queue = [ReviewUnit.model_validate(u) for u in _load_json(run_dir, "units.json") or []]
    queue_ids = {u.unit_id for u in queue}
    remaining = [u for u in prioritize(queue) if u.unit_id not in fully_done]
    if paths.strip():
        prefix = paths.strip().rstrip("/")
        remaining = [u for u in remaining if u.primary_file.startswith(prefix)]
    if more is not None and more < 0:
        # Same guard as --max-units: a negative value silently meaning "no
        # limit" once reviewed the whole queue when the caller meant to cap it.
        raise ConfigError(t("pipeline.max_units_negative", n=more))
    if more is not None:
        remaining = remaining[:more]
    return {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "mode": old.get("mode"),
        "reviewer_models": reviewer_models,
        "falsifier_model": old.get("falsifier_model"),
        "units_total": len(queue),
        "units_done": len(fully_done & queue_ids),
        "units_remaining_all": len(queue_ids - fully_done),
        "batch_size": len(remaining),
        "batch_unit_ids": [u.unit_id for u in remaining],
    }


def _seats_from_run(old_dump: dict) -> list[str]:
    """Every model id that held a seat in the original run.

    Reviewers, the falsifier, and — for tiered quick — the verifier that the
    comparison block remembers. Constraining the registry to exactly these
    models makes re-routing deterministic.
    """
    seats = list(old_dump.get("reviewer_models") or [])
    falsifier = old_dump.get("falsifier_model")
    if falsifier:
        seats.append(falsifier)
    verifier = (old_dump.get("comparison") or {}).get("verifier")
    if verifier:
        seats.append(verifier)
    return list(dict.fromkeys(seats))


def _subset_registry(registry: ModelRegistry, seats: list[str]) -> ModelRegistry:
    by_id = {m.model_id: m for m in registry.models}
    missing = [mid for mid in seats if mid not in by_id]
    if missing:
        raise ConfigError(t("resume.models_missing", models=", ".join(missing)))
    return ModelRegistry(models=[by_id[mid] for mid in seats])


def _merge_parse(old: dict | None, new: dict | None) -> dict:
    old = old or {}
    new = new or {}
    keys = ("calls", "first_try", "repaired", "failed")
    merged = {k: int(old.get(k, 0) or 0) + int(new.get(k, 0) or 0) for k in keys}
    total = merged.get("calls", 0)
    merged["success_rate"] = round((total - merged["failed"]) / total, 3) if total else None
    return merged


def resume_review(
    repo_root: Path,
    run_id: str,
    more: int | None = None,
    paths: str = "",
    out: Path | None = None,
    registry: ModelRegistry | None = None,
    progress=None,
    should_stop=None,
    on_findings=None,
    token_budget: int | None = None,
) -> ReviewOutcome:
    """Continue an existing run: same seats, remaining units, merged report.

    The merge is additive by design: the old findings were already clustered
    and falsified, the new batch is clustered and falsified within itself, and
    the two lists concatenate before ranking. Re-clustering across batches
    would need the raw per-model per-unit findings of sitting #1, which the
    artifacts deliberately do not keep — and cross-batch duplicates are rare
    because the batches cover disjoint units.

    A run-level file lock serializes concurrent sittings (CLI vs UI, two
    terminals): both would otherwise read the same old ledger and the later
    writer would silently drop the earlier batch's results.
    """
    emit = progress or (lambda _msg: None)

    def say(message: str) -> None:
        if should_stop and should_stop():
            raise ReviewCancelled(t("pipeline.stopped"))
        emit(message)

    repo_root = repo_root.resolve()
    run_dir = _run_dir(repo_root, run_id)
    if not run_dir.is_dir():
        raise LookupError(t("resume.no_report", run_id=run_id))
    # Take the run lock BEFORE reading any ledger. A second sitting that queues
    # here must plan from the first sitting's merged artifacts; planning from
    # the snapshot it read while waiting would re-review the same units and
    # write back stale findings after calls.json already marked them covered.
    lock = _run_lock(run_dir)
    try:
        old_dump = _load_json(run_dir, "findings.json")
        if not old_dump:
            raise LookupError(t("resume.no_report", run_id=run_id))
        old_collection = FindingCollection.model_validate(old_dump)
        reviewer_models = old_collection.reviewer_models or []
        if not reviewer_models:
            raise ConfigError(t("resume.no_seats"))
        mode = old_collection.mode or "quick"
        meta = _load_json(run_dir, "run_meta.json") or {}

        plan = plan_resume(repo_root, run_id, more=more, paths=paths)
        queue = [ReviewUnit.model_validate(u) for u in _load_json(run_dir, "units.json") or []]
        batch_ids = set(plan["batch_unit_ids"])
        batch = [u for u in prioritize(queue) if u.unit_id in batch_ids]
        if not batch:
            raise LookupError(t("resume.nothing_left", run_id=run_id))

        # Seats come from the run's own record, resolved against the *current*
        # config (which holds the base URLs / keys). A model id dropped from the
        # config is a hard error, never a silent substitution.
        config, _source = effective_config(repo_root)
        if config is None:
            raise ConfigError(str(t("config.no_models", path=".codeerratum/config.json")))
        pool = registry if registry is not None else config.registry
        seats = _seats_from_run(old_dump)
        run_registry = _subset_registry(pool, seats)
        run_mode = resolve_mode(mode, config)

        # Which pin to replay. For tiered quick the recorded reviewer is the
        # *scanner*; repinning it would promote the scanner to the verifier
        # seat and silently swap models between sittings. The user's original
        # pin (run_meta.models) is what produced the recorded seats, so it is
        # what must be replayed. Old runs without run_meta.json fall back to
        # the recorded reviewers.
        original_pin = [str(m).strip() for m in (meta.get("models") or []) if str(m).strip()]
        pin = original_pin or list(reviewer_models)
        original_author = str(meta.get("author_model") or "").strip() or None
        # "tiered" defaults to the recorded comparison, not to True: an old
        # run without run_meta.json was not necessarily tiered, and forcing
        # tiered routing on it would re-route its seats.
        if "tiered" in meta:
            tiered = bool(meta["tiered"])
        else:
            tiered = (old_dump.get("comparison") or {}).get("mode") == "tiered"

        # Validate the route once, here, so a wrong seat count fails before any
        # paid call instead of ten minutes in.
        try:
            if run_mode == "quick" and tiered:
                run_registry.route_quick_tier(pinned=pin, author_model=original_author)
            else:
                run_registry.route(run_mode, author_model=original_author, pinned=pin)
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc

        old_calls = _load_json(run_dir, "calls.json") or []
        old_diags = _load_json(run_dir, "static_diags.json") or []
        store = store_for(repo_root)

        say(
            t(
                "resume.start",
                done=plan["units_done"],
                total=plan["units_total"],
                batch=len(batch),
            )
        )

        # Sources for reviewer context. New runs record their diff spec in
        # run_meta.json so the resume can re-derive the same sources; older runs
        # (and whole-worktree runs, where sources are empty anyway) degrade to an
        # empty map — context is thinner, anchors are unaffected.
        sources: dict[str, str] = {}
        diff_spec = str(meta.get("diff_spec") or "").strip()
        if diff_spec and diff_spec.upper() not in ("WORKTREE", "ALL"):
            try:
                # The batch's own ``paths`` narrows which units re-run; the context
                # must be rebuilt from the *original* run's paths, or a resumed
                # unit loses the source snapshot the first sitting reviewed with.
                sources = new_side_sources(diff_spec, repo_root, paths=str(meta.get("paths") or ""))
            except Exception:  # noqa: BLE001 - degraded context beats a dead resume
                sources = {}

        # The new sitting must run under the same rules and language as the
        # original, or the two sittings' findings disagree on conventions and
        # output language while the report claims one consistent run. The
        # repo's rules file is the persistent source; flag-supplied rules from
        # the first sitting are not recoverable.
        rules = resolve_house_rules(None, repo_root)

        try:
            new_collection, new_calls, new_diags = asyncio.run(
                run_review(
                    run_registry,
                    run_mode,
                    batch,
                    repo_root,
                    run_id,
                    author_model=original_author,
                    rejected_category_counts=store.rejected_category_counts() or None,
                    house_rules=rules.text,
                    sources=sources,
                    progress=say,
                    should_stop=should_stop,
                    pinned=pin,
                    on_findings=on_findings,
                    tiered=tiered,
                    # Same reason the first sitting pins it at run start: the
                    # language must travel as a value, not leak through a
                    # ContextVar from whatever thread happens to resume.
                    output_lang=get_lang(),
                    # None asks the environment; an explicit 0 means uncapped.
                    token_budget=(
                        token_budget_from_env() if token_budget is None else token_budget
                    ),
                )
            )
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc

        # A re-run unit replaces ALL of its machine-side records from the earlier
        # sitting: the stale provider_error, any finding the partial data produced,
        # and any refuted verdict those partial findings earned. The fresh review
        # with both opinions is the authority for that unit; keeping the old state
        # would double-count it (one finding could appear twice under two ids).
        rerun_ids = {u.unit_id for u in batch}
        old_rejected = [r for r in old_collection.rejected if r.get("unit_id") not in rerun_ids]
        old_findings_kept = [f for f in old_collection.findings if f.unit_id not in rerun_ids]
        old_refuted_kept = [
            f for f in old_collection.refuted_findings if f.unit_id not in rerun_ids
        ]

        merged_findings = rank_findings(old_findings_kept + list(new_collection.findings))
        merged_rejected = old_rejected + list(new_collection.rejected)
        merged_refuted = old_refuted_kept + list(new_collection.refuted_findings)
        merged_calls = old_calls + list(new_calls)
        merged_diags = old_diags + list(new_diags)

        old_cmp = old_collection.comparison or {}
        new_cmp = new_collection.comparison or {}
        stopped_now = bool(new_collection.stopped)

        def _sum_int(key: str) -> int:
            return int((old_cmp.get(key) or 0) or 0) + int((new_cmp.get(key) or 0) or 0)

        raw_counts: dict[str, int] = {}
        for cmp_src in (old_cmp, new_cmp):
            for mid, count in (cmp_src.get("raw_counts") or {}).items():
                raw_counts[mid] = raw_counts.get(mid, 0) + int(count)

        comparison = dict(new_cmp)
        comparison["mode"] = mode
        comparison["raw_counts"] = raw_counts
        comparison["after_clustering"] = _sum_int("after_clustering")
        # Surviving findings are the merged list itself: re-run units replace their
        # old records above, so summing the two sittings would double-count.
        comparison["after_falsification"] = len(merged_findings)
        comparison["zones"] = {k: len(v) for k, v in _zone_split(merged_findings).items()}
        comparison["parse"] = _merge_parse(old_cmp.get("parse"), new_cmp.get("parse"))
        comparison["parse_failures"] = list(old_cmp.get("parse_failures") or []) + list(
            new_cmp.get("parse_failures") or []
        )
        # Coverage counts units this batch actually finished for every
        # reviewer — provider_error units have no call entries and will be
        # re-offered by the next resume, so counting them as done lied about
        # progress.
        new_done = _review_done_by_model(new_calls, reviewer_models)
        covered_now = set.intersection(*[new_done.get(m, set()) for m in reviewer_models])
        comparison["resumed"] = {
            "batch": len(batch),
            "units_done": plan["units_done"] + len(covered_now & rerun_ids),
            "units_total": plan["units_total"],
            "batches": int((old_cmp.get("resumed") or {}).get("batches") or 1) + 1,
        }
        if stopped_now and new_cmp.get("stopped"):
            comparison["stopped"] = new_cmp["stopped"]

        if rules:
            hr_sha, hr_chars, hr_source = rules.sha, rules.chars, rules.source
        else:
            # The rules file is gone but sitting #1 ran under one; keep the old
            # trace rather than claiming "no rules were applied".
            hr_sha = old_collection.house_rules_sha
            hr_chars = old_collection.house_rules_chars
            hr_source = old_collection.house_rules_source

        merged = FindingCollection(
            run_id=run_id,
            repo=old_collection.repo,
            commit=get_commit(repo_root),
            model_id=old_collection.model_id,
            spec_level=old_collection.spec_level,
            mode=mode,
            reviewer_models=reviewer_models,
            falsifier_model=old_collection.falsifier_model,
            comparison=comparison,
            findings=merged_findings,
            refuted_findings=merged_refuted,
            rejected=merged_rejected,
            stopped=stopped_now,
            house_rules_sha=hr_sha,
            house_rules_chars=hr_chars,
            house_rules_source=hr_source,
        )

        # Notices: what changed in this sitting, in the same vocabulary the
        # original report already uses — no new semantics for the reader to learn.
        notes = [
            t(
                "resume.note",
                batch=comparison["resumed"]["batch"],
                done=comparison["resumed"]["units_done"],
                total=comparison["resumed"]["units_total"],
            )
        ]
        provider_failures = [r for r in merged_rejected if r.get("reason") == "provider_error"]
        if provider_failures:
            notes.append(t("pipeline.units_failed", n=len(provider_failures)))
        drop_counts: dict[str, int] = {}
        for r in merged_rejected:
            reason = str(r.get("reason") or "unknown")
            if is_unit_failure(reason):
                continue
            drop_counts[reason] = drop_counts.get(reason, 0) + 1
        if drop_counts:
            top = sorted(drop_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
            notes.append(
                t(
                    "pipeline.machine_dropped",
                    n=sum(drop_counts.values()),
                    reasons="、".join(f"{reason}×{count}" for reason, count in top),
                )
            )
        merged.notes = notes

        # Replaced findings that survive nowhere in the merged report must not
        # linger in the feedback store: stats() and get_finding() would keep
        # counting and returning findings this run's report no longer contains.
        # Scoped to this run, and only ids the fresh review did NOT re-produce
        # (a re-produced id is re-inserted by save_run; deleting it first would
        # reset its human verdict to 'open').
        replaced_ids = [f.id for f in old_collection.findings if f.unit_id in rerun_ids]
        stale_ids = [fid for fid in replaced_ids if fid not in {f.id for f in merged_findings}]
        if stale_ids:
            store.delete_findings(stale_ids, run_id=run_id)

        ts = datetime.now(UTC).isoformat(timespec="seconds")
        markdown = render_markdown(merged, ts)
        # Atomic writes with calls.json LAST: it is the coverage ledger, so a
        # crash between files must leave it behind findings.json. Then the next
        # resume re-reviews (wasted tokens) instead of skipping units whose
        # findings never landed (permanent, silent loss).
        atomic_write_text(
            run_dir / "findings.json",
            json.dumps(merged.model_dump(mode="json"), ensure_ascii=False, indent=2),
        )
        atomic_write_text(
            run_dir / "static_diags.json", json.dumps(merged_diags, ensure_ascii=False, indent=2)
        )
        atomic_write_text(
            run_dir / "parse_stats.json",
            json.dumps(comparison.get("parse", {}), ensure_ascii=False, indent=2),
        )
        atomic_write_text(
            run_dir / "parse_failures.json",
            json.dumps(comparison.get("parse_failures", []), ensure_ascii=False, indent=2),
        )
        atomic_write_text(run_dir / "review.md", markdown)

        out_path: Path | None = None
        if out is not None:
            out_path = out if out.is_absolute() else repo_root / out
            atomic_write_text(out_path, markdown)

        atomic_write_text(
            run_dir / "calls.json", json.dumps(merged_calls, ensure_ascii=False, indent=2)
        )

        store.save_run(merged, ts)
        store.save_comparison(merged, ts)

        return ReviewOutcome(
            run_id=run_id,
            run_dir=run_dir,
            collection=merged,
            units_total=plan["units_total"],
            units_selected=len(batch),
            units=batch,
            calls=merged_calls,
            diags=merged_diags,
            markdown=markdown,
            out_path=out_path,
            created_at=ts,
        )
    finally:
        _release_run_lock(lock)
