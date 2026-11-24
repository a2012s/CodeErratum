"""Shared review pipeline — the single code path behind both the CLI and the web UI.

The GUI must not re-implement diff -> units -> orchestrate -> persist; a second
copy would silently drift from the CLI (different caps, different artifacts,
different error handling). Everything in here is UI-agnostic: callers pass a
progress callback and format the results themselves.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .config import Config
from .config_store import CONFIG_REL
from .config_store import effective as effective_config
from .i18n import get_lang, t
from .ingest import (
    DiffError,
    get_commit,
    get_diff,
    is_git_repo,
    is_head_family,
    new_side_sources,
    snapshot_diff,
    split_diff,
    wants_full_worktree,
)
from .models import FindingCollection, ModelRegistry, ReviewUnit
from .models.finding import is_unit_failure
from .repo_ignore import ensure_state_dir_ignored
from .report import render_markdown
from .review import (
    MAX_HOUSE_RULES_CHARS,
    ReviewCancelled,
    run_review,
    sanitize_house_rules,
    truncate_house_rules,
)
from .spec import detect as detect_spec_level
from .store import ReviewStore

STATE_DIR = ".codeerratum"
HOUSE_RULES_FILE = f"{STATE_DIR}/rules.md"
VALID_MODES = ("quick", "dual", "committee")


def atomic_write_text(path: Path, text: str) -> None:
    """Write a file via temp + os.replace so a crash can never leave half a file.

    The run ledger (calls.json / findings.json) is read back by resume; a torn
    write there poisons the coverage calculation and silently loses findings.
    Windows os.replace is atomic within one volume, same as POSIX rename.
    """
    # A fixed ".tmp" name is shared by every writer of the same path: two
    # concurrent runs could replace each other's temp file before either
    # renamed it. mkstemp gives each writer its own inode.
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


class PipelineError(Exception):
    """Base class for user-facing failures the CLI/UI can report without a traceback."""


class NotARepository(PipelineError):
    pass


class DiffFailed(PipelineError):
    pass


class NoChanges(PipelineError):
    """Raised when the diff is empty — not an error for the user, just nothing to do."""


class ConfigError(PipelineError):
    pass


@dataclass
class ReviewOutcome:
    run_id: str
    run_dir: Path
    collection: FindingCollection
    units_total: int
    units_selected: int
    units: list[ReviewUnit] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)
    diags: list[dict] = field(default_factory=list)
    markdown: str = ""
    out_path: Path | None = None
    created_at: str = ""

    @property
    def latency_ms_avg(self) -> float:
        if not self.calls:
            return 0.0
        return sum(c.get("latency_ms", 0) or 0 for c in self.calls) / len(self.calls)

    def token_totals(self) -> dict:
        """Sum per-call usage into one report, split by model for cost attribution.

        Usage was already captured on every call (``completion.usage.model_dump()``);
        it was just never rolled up. Cached tokens are counted inside input tokens
        by the providers, so they are reported separately but not added again.
        """
        total_in = total_out = total_cached = 0
        by_model: dict[str, dict[str, int]] = {}
        for call in self.calls:
            usage = call.get("usage") or {}
            i = int(usage.get("input_tokens", 0) or 0)
            o = int(usage.get("output_tokens", 0) or 0)
            cached = int(usage.get("cached_tokens", 0) or 0)
            total_in += i
            total_out += o
            total_cached += cached
            model = str(call.get("model") or "unknown")
            row = by_model.setdefault(
                model, {"input": 0, "output": 0, "cached": 0, "total": 0, "calls": 0}
            )
            row["input"] += i
            row["output"] += o
            row["cached"] += cached
            row["total"] += i + o
            row["calls"] += 1
        return {
            "input": total_in,
            "output": total_out,
            "cached": total_cached,
            "total": total_in + total_out,
            "by_model": by_model,
        }

    def summary(self) -> dict:
        # "dropped" counts only findings the machine removed (refuted, anchor
        # mismatch, duplicate, invalid_finding). Unit call failures never
        # removed a finding — verify_error even keeps it — so they are counted
        # separately as failed_units. "rejected" is kept as a legacy alias of
        # "dropped"; it never meant a human rejection.
        dropped = sum(
            1 for r in self.collection.rejected if not is_unit_failure(str(r.get("reason") or ""))
        )
        failed_units = len(self.collection.rejected) - dropped
        return {
            "run_id": self.run_id,
            "created_at": self.created_at,
            "mode": self.collection.mode,
            "commit": self.collection.commit,
            "models": self.collection.reviewer_models,
            "falsifier": self.collection.falsifier_model,
            "findings": len(self.collection.findings),
            "stopped": bool(self.collection.stopped),
            "dropped": dropped,
            "failed_units": failed_units,
            "rejected": dropped,  # legacy alias; read "dropped" in new code
            "units_total": self.units_total,
            "units_selected": self.units_selected,
            "calls": len(self.calls),
            "static_diags": len(self.diags),
            "latency_ms_avg": round(self.latency_ms_avg, 1),
            "tokens": self.token_totals(),
            "zones": {k: len(v) for k, v in self.collection.zoned().items()},
            "comparison": self.collection.comparison,
            "parse": self.collection.comparison.get("parse", {}),
            "notes": self.collection.notes,
            "house_rules": {
                "applied": bool(self.collection.house_rules_sha),
                "sha": self.collection.house_rules_sha,
                "chars": self.collection.house_rules_chars,
                "source": self.collection.house_rules_source,
            },
        }


def find_repo_root(path: Path) -> Path:
    """Nearest ancestor containing .git, or the given path when there is none."""
    current = path.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return current


def store_for(repo_root: Path) -> ReviewStore:
    # Review history and run artefacts land in the reviewed repository, so the
    # exclude rule is set up here too — not only when the UI saves a config.
    ensure_state_dir_ignored(repo_root)
    return ReviewStore(repo_root / STATE_DIR / "feedback.db")


def prioritize(units: list[ReviewUnit]) -> list[ReviewUnit]:
    """Spend the unit budget on risky, public, larger changes first."""
    return sorted(
        units,
        key=lambda u: (
            0 if u.risk_tags else 1,
            0 if u.is_public else 1,
            -u.changed_lines,
        ),
    )


# "Decide for me" covers the ordinary case: a normal diff is a few dozen units,
# and stopping at 60 keeps a mechanical full sweep from quietly turning into a
# 600-call bill. Anything bigger is a deliberate "review everything" (0), and the
# notice says which one happened.
AUTO_UNIT_BUDGET = 60

# Docs, manifests and translation catalogs are not where review tokens pay off:
# they have no executable semantics, and an LLM "finding" there is usually a
# style opinion. Static checks (schema/lint/doc-drift) cover them better. The
# skip is visible in the report notes and reversible with include_non_code=True.
NON_CODE_SUFFIXES = {
    ".md",
    ".mdx",
    ".rst",
    ".adoc",
    ".txt",
    ".json",
    ".toml",
    ".yaml",
    ".yml",
    ".ini",
}
I18N_PATH_PARTS = ("/i18n/", "/locale/", "/locales/", "/translations/", "/lang/")


def is_non_code_unit(unit: ReviewUnit) -> bool:
    """True for documentation, config manifests and translation catalogs."""
    path = f"/{unit.primary_file.replace(chr(92), '/').lower()}"
    if Path(path).suffix in NON_CODE_SUFFIXES:
        return True
    if any(part in path for part in I18N_PATH_PARTS):
        return True
    return unit.kind == "config"


def available_models(repo_root: Path) -> list[dict]:
    """Configured models, for the UI picker. Ids only — never the api_key.

    Same precedence as a review (saved config wins over the environment), so the
    list the user picks from is exactly the list a run would use.
    """
    # Same resolution as a real run (project file → user default → env), so the
    # picker never offers a different roster than the one a run would use.
    try:
        config, _source = effective_config(repo_root)
    except ValueError:
        return []
    if config is None:
        return []
    return [
        {
            "id": m.model_id,
            "family": m.resolved_family(),
            "measured_n": m.measured_n,
        }
        for m in config.registry.models
    ]


def _unit_budget(max_units: int | None, total: int) -> tuple[int, str]:
    """Resolve the unit budget and say how it was decided.

    ``None`` → auto, ``0`` → everything, positive → literal. The second element
    only picks which notice to print, so the report can explain itself.

    Negative values are rejected instead of falling into the ``<= 0`` branch:
    ``--max-units -1`` once silently reviewed the whole diff — the opposite of
    what a cost guard is for, and indistinguishable from intent.
    """
    if max_units is None:
        return min(total, AUTO_UNIT_BUDGET), "auto"
    if max_units < 0:
        raise ConfigError(t("pipeline.max_units_negative", n=max_units))
    if max_units == 0:
        return total, "all"
    return max_units, "fixed"


def resolve_mode(mode: str, config: Config) -> str:
    resolved = (mode or config.mode).strip().lower()
    if resolved not in VALID_MODES:
        raise ConfigError(f"Invalid mode {resolved!r}; choose quick/dual/committee")
    return resolved


def abstain_warning(findings: list) -> str:
    """Return a notice when too many falsification verdicts were 'insufficient_context'.

    A high abstain rate is a signal about *our* context, not about the code: the
    falsifier keeps saying it cannot decide. Surfacing it turns a silent degradation
    into a number the user can act on.
    """
    # Only the falsification round can abstain. Counting reviewer `assert`
    # verdicts in the denominator dilutes a total context failure into quiet
    # noise: two asserts plus one abstain used to read as 1/3 and stay silent.
    roles = [
        verdict.role
        for f in findings
        for verdict in f.verdicts
        if verdict.role in ("confirm", "refute", "abstain")
    ]
    if not roles:
        return ""
    abstained = sum(1 for role in roles if role == "abstain")
    if abstained / len(roles) <= 0.3:
        return ""
    return t("pipeline.abstain", n=abstained, total=len(roles))


def apply_measured_profiles(registry: ModelRegistry, store: ReviewStore) -> int:
    """Fold the accumulated per-model record back into routing scores.

    Confidence is deliberately conservative: ``ModelSpec.score`` ignores the measured
    value below 30 samples, and precision needs five decided verdicts before the
    ratio means anything. A routing change driven by three data points is worse than
    no routing change at all.
    """
    profiles = store.model_profile_map()
    applied = 0
    for spec in registry.models:
        profile = profiles.get(spec.model_id)
        if not profile or not profile["n_assert"]:
            continue
        spec.measured_n = int(profile["n_assert"])
        spec.measured_unique_rate = profile["n_unique"] / profile["n_assert"]
        decided = profile["n_confirmed"] + profile["n_refuted"]
        spec.measured_precision = profile["n_confirmed"] / decided if decided >= 5 else None
        applied += 1
    return applied


@dataclass
class HouseRules:
    """The owner's additions to the system prompt, plus the trace needed to explain a run."""

    text: str = ""
    source: str = ""
    sha: str = ""
    chars: int = 0
    truncated: bool = False

    def __bool__(self) -> bool:
        return bool(self.text)

    def summary(self) -> dict:
        return {
            "applied": bool(self.text),
            "source": self.source,
            "sha": self.sha,
            "chars": self.chars,
            "truncated": self.truncated,
        }


def resolve_house_rules(explicit: str | None, repo_root: Path) -> HouseRules:
    """Explicit text wins; otherwise `<repo>/.codeerratum/rules.md` is picked up.

    Blank explicit input falls back to the file on purpose: the UI textarea starts
    empty, and a hidden third state ("empty means disable") would make behaviour
    depend on whether a file happens to exist.
    """
    text, source = "", ""
    if explicit and explicit.strip():
        text, source = explicit, "flag"
    else:
        path = repo_root / HOUSE_RULES_FILE
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            source = HOUSE_RULES_FILE
    text, truncated = truncate_house_rules(sanitize_house_rules(text))
    if not text:
        return HouseRules()
    return HouseRules(
        text=text,
        source=source,
        sha=hashlib.sha256(text.encode("utf-8")).hexdigest()[:12],
        chars=len(text),
        truncated=truncated,
    )


def execute_review(
    repo_root: Path,
    diff_spec: str = "HEAD~1",
    mode: str = "",
    max_units: int | None = None,
    author_model: str | None = None,
    models: list[str] | None = None,
    out: Path | None = None,
    registry: ModelRegistry | None = None,
    progress: Callable[[str], None] | None = None,
    house_rules: str = "",
    paths: str = "",
    should_stop: Callable[[], bool] | None = None,
    on_findings: Callable[[object, list, str], None] | None = None,
    include_non_code: bool = False,
    tiered: bool = True,
) -> ReviewOutcome:
    """Run the whole pipeline and persist artifacts + feedback rows.

    ``out`` writes an extra copy of the work order (the CLI's ``-o``). The run
    directory always keeps its own ``review.md`` so the UI can serve a run
    without touching the working tree. ``paths`` narrows the diff to a git
    pathspec (e.g. ``src/api``), so a review can target one folder without
    staging changes first. ``should_stop`` is polled by the orchestrator; when
    it turns true, in-flight model calls are cancelled. Units that already
    finished are kept and a partial ``review.md`` is written with
    ``stopped=True``; only a stop before any unit finished raises
    ReviewCancelled (there is nothing to report).

    ``max_units=None`` means "decide for me" (see ``AUTO_UNIT_BUDGET``), ``0``
    means every unit, and a positive number is taken literally. ``models`` pins
    the review seats by model id instead of letting the scorer choose.

    ``on_findings(unit, findings, model_id)`` fires per reviewed unit, before the
    run is over, so a caller can stream results as they land. Nothing downstream
    depends on it: the report is still built from the full collection, so a
    listener that returns early only affects what the caller chose to display.

    ``include_non_code=False`` drops docs/config/i18n units before the budget is
    applied. ``tiered=True`` (quick mode only) scans every unit with the lightest
    configured model and re-reviews hit units with the strong one; it degrades to
    a normal single-model quick run when no second model is configured.
    """
    emit = progress or (lambda _msg: None)

    def say(message: str) -> None:
        # Check at every lifecycle message too, not just inside model calls —
        # diff splitting / report writing have no other cancellation point.
        if should_stop and should_stop():
            raise ReviewCancelled(t("pipeline.stopped"))
        emit(message)

    repo_root = repo_root.resolve()
    # Decide what the range means before loading models: a folder that cannot
    # support the requested revision is a cheaper and clearer failure than
    # "missing API key", and it is the question the user actually asked.
    run_spec = diff_spec.strip() or "HEAD"
    snapshot = wants_full_worktree(run_spec, repo_root)
    explicit_snapshot = run_spec.strip().upper() in ("WORKTREE", "ALL")
    if not snapshot and not is_git_repo(repo_root):
        raise NotARepository(t("server.not_a_repo", path=repo_root, spec=run_spec))

    # A saved config (written by the UI) wins over env so an edit sticks across
    # process restarts. A corrupt file is an error, never a silent fallback —
    # otherwise the user thinks the old model set is still in force.
    try:
        config, _source = effective_config(repo_root)
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read {CONFIG_REL}: {exc}") from exc
    if config is None:
        raise ConfigError(str(t("config.no_models", path=CONFIG_REL)))
    if registry is not None:
        config = Config(
            base_url=config.base_url,
            api_key=config.api_key,
            model=config.model,
            registry=registry,
            mode=config.mode,
        )
    run_mode = resolve_mode(mode, config)
    if models:
        # Validate here, before the diff is read: a typo in a model id should be a
        # clear error at submit time, not a 500 ten minutes into a run.
        try:
            config.registry.route(run_mode, pinned=models)  # type: ignore[arg-type]
        except ValueError as exc:
            raise ConfigError(t("pipeline.models_invalid", detail=str(exc))) from exc

    # A project that was never committed has no revision to diff against, but it
    # does have code. Snapshot the working tree instead of failing at "no
    # changes" — the user asked to review the code, not to see git's opinion.
    try:
        diff_text = (
            snapshot_diff(repo_root, paths=paths)
            if snapshot
            else get_diff(run_spec, repo_root, paths=paths)
        )
    except DiffError as exc:
        raise DiffFailed(str(exc)) from exc

    # Everything committed, working tree clean: HEAD is legitimately empty, but
    # the code is still on disk and the user just pressed Review. Answering
    # "Nothing to review" is technically correct and completely useless — it was
    # the third time this exact wall came back. Fall back to the whole worktree
    # and say so, instead of making the user discover the scope switch.
    scope_fallback = False
    if not diff_text.strip() and not snapshot and is_head_family(run_spec):
        try:
            whole_tree = snapshot_diff(repo_root, paths=paths)
        except DiffError:
            whole_tree = ""
        if whole_tree.strip():
            diff_text = whole_tree
            snapshot = True
            scope_fallback = True

    if not diff_text.strip():
        raise NoChanges(
            t("pipeline.snapshot_empty") if snapshot else "No changes in diff. Nothing to review."
        )

    run_id = uuid.uuid4().hex[:12]
    run_dir = repo_root / STATE_DIR / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "input.diff").write_text(diff_text, encoding="utf-8")
    # The resume path re-derives reviewer context from this record: a run's
    # sources depend on the diff spec and pathscope of the sitting that
    # produced it, and neither is recoverable from units.json alone.
    (run_dir / "run_meta.json").write_text(
        json.dumps(
            {
                "diff_spec": run_spec,
                "snapshot": snapshot,
                "mode": run_mode,
                "models": list(models or []),
                "author_model": author_model or None,
                "tiered": tiered,
                "paths": paths,
                "include_non_code": include_non_code,
                "max_units": max_units,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    notices: list[str] = []
    announced: set[str] = set()

    def announce(message: str) -> None:
        """Record a notice and show it once, now.

        The list is replayed after the split for anything ``split_diff`` added
        (skipped files); without this bookkeeping every early notice printed
        twice — once live and once in the replay.
        """
        notices.append(message)
        announced.add(message)
        say(message)

    if scope_fallback:
        announce(t("pipeline.scope_fallback", spec=run_spec))
    elif snapshot:
        announce(
            t(
                "pipeline.worktree_requested"
                if explicit_snapshot
                else "pipeline.no_commits_snapshot"
            )
        )
    if paths.strip():
        announce(t("pipeline.path_scope", paths=paths.strip()))
    rules = resolve_house_rules(house_rules, repo_root)
    if rules.truncated:
        announce(t("pipeline.rules_truncated", limit=MAX_HOUSE_RULES_CHARS))
    if rules:
        say(f"Project rules: {rules.source} ({rules.chars} chars, sha {rules.sha})")
    # Read the new side from the diff revision, not the working tree: reviewing
    # main...feature while on main used to slice feature's line numbers into
    # main's text, and the anchor hash happily confirmed the wrong region.
    if snapshot:
        sources: dict[str, str] = {}  # the new side *is* the working tree
    else:
        try:
            sources = new_side_sources(run_spec, repo_root, paths=paths)
        except DiffError as exc:
            sources = {}
            announce(t("pipeline.snapshot_fallback", error=exc))
    units = split_diff(diff_text, repo_root, notices=notices, sources=sources)
    if not include_non_code:
        before = len(units)
        units = [u for u in units if not is_non_code_unit(u)]
        if before > len(units):
            announce(t("pipeline.non_code_skipped", n=before - len(units)))
    (run_dir / "units.json").write_text(
        json.dumps([u.model_dump() for u in units], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    say(f"Split diff into {len(units)} review unit(s)")
    for note in notices:
        if note not in announced:
            say(note)
    if not units:
        # No unit means nothing was ever persisted except the empty queue; an
        # abandoned run directory would pollute the run list forever.
        shutil.rmtree(run_dir, ignore_errors=True)
        raise NoChanges("No reviewable units found.")

    # A budget of 0 means "no budget": every unit the diff produced. The cap is
    # there to stop a typo from launching a thousand calls, not to hide work
    # the user asked for — 332 units of 332 is a legitimate request.
    budget, how = _unit_budget(max_units, len(units))
    selected = prioritize(units)[:budget]
    if len(units) > budget:
        announce(
            t(
                "pipeline.units_auto" if how == "auto" else "pipeline.units_capped",
                kept=budget,
                total=len(units),
            )
        )

    say(f"Mode: {run_mode} | models: {len(config.registry.models)}")

    store = store_for(repo_root)
    profiled = apply_measured_profiles(config.registry, store)
    if profiled:
        say(f"Model profiles: {profiled} model(s) have accumulated history")
    rejected_counts = store.rejected_category_counts()
    if rejected_counts:
        say(f"Historical rejects: {rejected_counts}")

    scan = (repo_root / "src") if (repo_root / "src").is_dir() else None
    spec_level, spec_reason = detect_spec_level(repo_root, scan=scan)
    say(f"Spec level: {spec_level} ({spec_reason})")

    try:
        collection, calls, all_diags = asyncio.run(
            run_review(
                config.registry,
                run_mode,  # type: ignore[arg-type]
                selected,
                repo_root,
                run_id,
                author_model=author_model or None,
                rejected_category_counts=rejected_counts or None,
                house_rules=rules.text,
                sources=sources,
                progress=say,
                should_stop=should_stop,
                pinned=models,
                on_findings=on_findings,
                tiered=tiered,
                # Pinned at run start: the review outlives the request thread that
                # seeded the language ContextVar, so it must travel as a value.
                output_lang=get_lang(),
            )
        )
        # A stop request must not abort persistence: once any unit finished,
        # the partial result is the only thing the user paid for. The stop flag
        # still shortens the run inside review, but report writing always
        # completes.
        emit(t("pipeline.report_writing"))
    except ReviewCancelled:
        # ReviewCancelled only escapes when zero units finished, so the run
        # directory holds no report — remove it instead of leaving an
        # unresumable shell in the run list.
        shutil.rmtree(run_dir, ignore_errors=True)
        raise
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc

    if collection.stopped:
        stopped_info = collection.comparison.get("stopped") or {}
        done = stopped_info.get("units_completed", 0)
        partial = stopped_info.get("units_partial", 0)
        total = stopped_info.get("units_total", len(selected))
        if partial:
            notices.append(
                t(
                    "pipeline.stopped_partial_mixed",
                    done=done,
                    partial=partial,
                    total=total,
                )
            )
        else:
            notices.append(t("pipeline.stopped_partial", done=done, total=total))

    # Two failure shapes must not share one sentence: provider_error means the
    # reviewer never produced anything (unit unreviewed), verify_error means
    # the finding survived but its second opinion is missing. Counting them
    # together made users underestimate coverage.
    provider_failures = [r for r in collection.rejected if r.get("reason") == "provider_error"]
    if provider_failures:
        notices.append(t("pipeline.units_failed", n=len(provider_failures)))
        # A count alone leaves the reader guessing between "my key is wrong" and
        # "the endpoint was slow". The hints are already human sentences.
        reasons: list[str] = []
        for r in provider_failures:
            hint = r.get("error_hint") or r.get("error") or ""
            if hint and hint not in reasons:
                reasons.append(hint)
        if reasons:
            notices.append(t("pipeline.units_failed_reason", reasons=" · ".join(reasons[:3])))

    verify_failures = [r for r in collection.rejected if r.get("reason") == "verify_error"]
    if verify_failures:
        notices.append(t("pipeline.verify_failed", n=len(verify_failures)))
        reasons_v: list[str] = []
        for r in verify_failures:
            hint = r.get("error_hint") or r.get("error") or ""
            if hint and hint not in reasons_v:
                reasons_v.append(hint)
        if reasons_v:
            notices.append(t("pipeline.units_failed_reason", reasons=" · ".join(reasons_v[:3])))

    # Only per-finding drops count as "machine dropped": refuted claims, anchor
    # mismatches, duplicates. Unit failures above never removed a finding, so
    # including them here would contradict the finding count in the same report
    # (one run could read "1 finding" and "1 dropped" for the same finding).
    drop_counts: dict[str, int] = {}
    for r in collection.rejected:
        reason = str(r.get("reason") or "unknown")
        if is_unit_failure(reason):
            continue
        drop_counts[reason] = drop_counts.get(reason, 0) + 1
    if drop_counts:
        top = sorted(drop_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
        notices.append(
            t(
                "pipeline.machine_dropped",
                n=sum(drop_counts.values()),
                reasons="、".join(f"{reason}×{count}" for reason, count in top),
            )
        )

    # A high abstain rate is a signal about *our* context, not about the code: the
    # falsifier keeps saying it cannot decide. Surfacing it turns a silent
    # degradation into a number the user can act on.
    warning = abstain_warning(collection.findings)
    if warning:
        notices.append(warning)

    parse_report = collection.comparison.get("parse", {})
    if parse_report.get("failed", 0):
        notices.append(
            t(
                "pipeline.parse_failures",
                n=parse_report["failed"],
                rate=parse_report.get("success_rate") or "n/a",
            )
        )

    collection.notes = notices
    collection.commit = get_commit(repo_root)
    collection.spec_level = spec_level
    collection.spec_level_reason = spec_reason
    collection.house_rules_sha = rules.sha
    collection.house_rules_chars = rules.chars
    collection.house_rules_source = rules.source

    atomic_write_text(
        run_dir / "findings.json",
        json.dumps(collection.model_dump(mode="json"), ensure_ascii=False, indent=2),
    )
    atomic_write_text(
        run_dir / "static_diags.json",
        json.dumps(all_diags, ensure_ascii=False, indent=2),
    )
    # A "parse success rate is a hard gate" claim that only exists in memory
    # cannot be audited. Persist both the counters and a bounded failure sample.
    atomic_write_text(
        run_dir / "parse_stats.json",
        json.dumps(collection.comparison.get("parse", {}), ensure_ascii=False, indent=2),
    )
    atomic_write_text(
        run_dir / "parse_failures.json",
        json.dumps(collection.comparison.get("parse_failures", []), ensure_ascii=False, indent=2),
    )

    ts = datetime.now(UTC).isoformat(timespec="seconds")
    markdown = render_markdown(collection, ts)
    atomic_write_text(run_dir / "review.md", markdown)
    out_path: Path | None = None
    if out is not None:
        out_path = out if out.is_absolute() else repo_root / out
        atomic_write_text(out_path, markdown)

    # calls.json is the coverage ledger: plan_resume derives "already done"
    # from it. Write it LAST so a crash between files leaves the ledger behind
    # the findings — the next resume then re-reviews (costs tokens) instead of
    # treating units as covered whose findings were never written (loses data).
    atomic_write_text(run_dir / "calls.json", json.dumps(calls, ensure_ascii=False, indent=2))

    store.save_run(collection, ts)
    store.save_comparison(collection, ts)

    return ReviewOutcome(
        run_id=run_id,
        run_dir=run_dir,
        collection=collection,
        units_total=len(units),
        units_selected=len(selected),
        units=selected,
        calls=calls,
        diags=all_diags,
        markdown=markdown,
        out_path=out_path,
        created_at=ts,
    )
