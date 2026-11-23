"""Phase-3 orchestrator — heterogeneous parallel review + aggregation + falsification.

The outer structure stays a fixed, reproducible pipeline (this is an agentic
workflow, not an autonomous agent): all models review in parallel, findings are
clustered, then high-value findings are falsified by a different-vendor model.

Backward compatible: mode='quick' delegates to the single-model review_unit path.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable
from pathlib import Path

import httpx

from ..context import build_context
from ..evidence import collect_static, known_facts_block
from ..i18n import t
from ..models import Finding, FindingCollection, ModelRegistry, ModelSpec, ReviewUnit, Verdict
from ..providers import OpenAICompatProvider, Provider
from .aggregator import agreement, cluster_findings, rank_findings, same_issue
from .executor import render_callers, review_unit
from .falsification import falsify_finding, should_falsify


class ReviewCancelled(Exception):
    """The user stopped the run. Distinct from a failure: no error dialog, no report."""


def _one_line(text: str) -> str:
    """Keep a full claim intact but flatten embedded newlines into one log frame."""
    return " ".join(text.split())


def _failure_text(exc: Exception, provider: Provider) -> str:
    """Turn a transport exception into something a log line can explain.

    ``ReadTimeout`` on its own answers none of the questions a user actually has
    ("did it hang? is my key wrong? did it retry?"). These strings name the
    window that expired and point at the fix.
    """
    if isinstance(exc, httpx.ReadTimeout):
        seconds = getattr(provider, "read_timeout", 0) or 0
        return t("orch.err_read_timeout", seconds=f"{seconds:.0f}")
    if isinstance(exc, (httpx.ConnectTimeout, httpx.ConnectError)):
        return t("orch.err_connect")
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in (401, 403):
            return t("orch.err_auth", status=status)
        if status == 429:
            return t("orch.err_rate_limited")
        if 500 <= status < 600:
            return t("orch.err_server", status=status)
        # 4xx other than auth/rate-limit: the status code is the whole message,
        # and it is identical in both languages by design.
        return f"HTTP {status}"
    if isinstance(exc, httpx.TransportError):
        return t("orch.err_transport", kind=type(exc).__name__)
    return type(exc).__name__


UNIT_CONCURRENCY_ENV = "CODEERRATUM_UNIT_CONCURRENCY"
DEFAULT_UNIT_CONCURRENCY = 4


def unit_concurrency() -> int:
    """How many model calls may be in flight at once, per model.

    One review unit is 60–120 s of generation and the calls are pure I/O wait, so
    running them one after another turned a 44-unit review into an hour of wall
    time for exactly the same tokens. The ceiling is not about local CPU: it is
    there to stay under a provider's rate limit. Override with
    ``CODEERRATUM_UNIT_CONCURRENCY`` (set 1 for a strictly serial run).
    """
    raw = os.environ.get(UNIT_CONCURRENCY_ENV, "").strip()
    if not raw:
        return DEFAULT_UNIT_CONCURRENCY
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_UNIT_CONCURRENCY
    return value if value > 0 else DEFAULT_UNIT_CONCURRENCY


def _make_provider(spec: ModelSpec) -> OpenAICompatProvider:
    provider = OpenAICompatProvider(spec.base_url, spec.api_key)
    # family is set per-instance so two OpenAI-compatible endpoints from different
    # vendors are still recognized as heterogeneous by the registry (not by provider).
    provider.family = spec.resolved_family()
    return provider


def _context_for(
    unit: ReviewUnit, repo_root: Path, sources: dict[str, str] | None = None
) -> tuple[str, str]:
    ctx = build_context(unit, repo_root, sources)
    diags = collect_static(unit.primary_file, repo_root)
    callers = ctx.get("callers") or []
    context = "\n\n".join(
        part
        for part in [
            ctx.get("enclosing", ""),
            render_callers(callers, ctx.get("caller_snippets")) if callers else "",
        ]
        if part
    )
    return context, known_facts_block(diags)


async def _review_with(
    provider: Provider,
    spec: ModelSpec,
    units: list[ReviewUnit],
    repo_root: Path,
    run_id: str,
    house_rules: str = "",
    sources: dict[str, str] | None = None,
    progress: Callable[[str], None] | None = None,
    on_findings: Callable[[ReviewUnit, list[Finding], str], None] | None = None,
    output_lang: str = "",
) -> tuple[dict[str, list[Finding]], list[dict], list[dict], list[dict]]:
    """Run one model over all units, a bounded number at a time.

    Returns its findings + rejected + call stats + diags. Completion order is not
    unit order once the gate is wider than one, so the "done" counter is a
    progress count rather than an index.

    ``on_findings`` is called per unit, as soon as that unit's findings are
    parsed — the UI streams them while the run is still going instead of staring
    at "unit 12/53" for minutes. It is presentation, so a listener that throws
    is swallowed: a broken log must never abandon a run someone is paying for.
    """
    say = progress or (lambda _msg: None)
    per_unit: dict[str, list[Finding]] = {}
    rejected: list[dict] = []
    calls: list[dict] = []
    diags_all: list[dict] = []
    total = len(units)
    gate = asyncio.Semaphore(unit_concurrency())
    finished = 0
    counter = asyncio.Lock()

    async def one(idx: int, unit: ReviewUnit) -> None:
        nonlocal finished
        async with gate:
            say(
                t(
                    "orch.unit_start",
                    model=spec.model_id,
                    idx=idx,
                    total=total,
                    file=unit.primary_file,
                )
            )
            try:
                findings, rej, completion, diags = await review_unit(
                    provider,
                    spec.model_id,
                    unit,
                    repo_root,
                    run_id,
                    house_rules,
                    sources,
                    output_lang=output_lang,
                )
            except Exception as exc:  # noqa: BLE001 - one model/unit failing must not kill the run
                rejected.append(
                    {
                        "unit_id": unit.unit_id,
                        "model": spec.model_id,
                        "reason": "provider_error",
                        "error": str(exc),
                        # Readable version for the report/notes; `error` keeps the
                        # raw exception text for whoever debugs the endpoint.
                        "error_hint": _failure_text(exc, provider),
                    }
                )
                say(
                    t(
                        "orch.unit_failed",
                        model=spec.model_id,
                        idx=idx,
                        total=total,
                        error=_failure_text(exc, provider),
                    )
                )
                return
            per_unit.setdefault(unit.unit_id, []).extend(findings)
            if on_findings and findings:
                try:
                    on_findings(unit, findings, spec.model_id)
                except Exception as exc:  # noqa: BLE001 - presentation cannot fail the run
                    # Swallowed so a broken listener cannot abandon a paid run —
                    # but not silently: a mis-wired callback looks exactly like
                    # "the UI shows nothing", which is the bug it exists to fix.
                    print(
                        f"[orchestrator] on_findings failed: {type(exc).__name__}: {exc}",
                        file=sys.stderr,
                    )
            for r in rej:
                r = dict(r)
                r["model"] = spec.model_id
                rejected.append(r)
            diags_all.extend(diags)
            calls.append(
                {
                    "model": spec.model_id,
                    "family": spec.resolved_family(),
                    "unit_id": unit.unit_id,
                    "latency_ms": completion.latency_ms,
                    "usage": completion.usage.model_dump(),
                    "raw_request_hash": completion.raw_request_hash,
                }
            )
            async with counter:
                finished += 1
                done = finished
            say(
                t(
                    "orch.unit_done",
                    model=spec.model_id,
                    idx=done,
                    total=total,
                    n=len(findings),
                )
            )

    await asyncio.gather(*(one(idx, unit) for idx, unit in enumerate(units, 1)))
    return per_unit, rejected, calls, diags_all


def _merge_verification(
    merged: list[Finding], verified: list[Finding], scanner_id: str, verifier_id: str
) -> tuple[list[Finding], int]:
    """Fold a strong model's re-review of hit units back into scanner findings.

    A finding both models reported gains a second ``assert`` verdict (and the
    ``unanimous`` label); a new finding only the verifier found is appended as
    ``unique``. Verdict roles — not a vote count — drive agreement, so a
    ``confirm`` here would be wrong: the verifier did review the code itself.
    """
    extra: list[Finding] = []
    for vf in verified:
        match = next((f for f in merged if same_issue(vf, f)), None)
        if match is not None:
            for mid in (scanner_id, verifier_id):
                if not any(v.model_id == mid and v.role == "assert" for v in match.verdicts):
                    match.verdicts.append(Verdict(model_id=mid, role="assert", reason=match.claim))
            if verifier_id not in match.source_models:
                match.source_models.append(verifier_id)
            match.agreement = agreement(match.verdicts)
            continue
        vf.verdicts.append(Verdict(model_id=verifier_id, role="assert", reason=vf.claim))
        vf.source_models = [verifier_id]
        vf.agreement = "unique"
        extra.append(vf)
    merged.extend(extra)
    return merged, len(extra)


def _flatten(per_unit: dict[str, list[Finding]], model_id: str) -> list[Finding]:
    out: list[Finding] = []
    for findings in per_unit.values():
        for f in findings:
            if not f.source_models:
                f.source_models = [model_id]
            out.append(f)
    return out


async def run_review(
    registry: ModelRegistry,
    mode: str,
    units: list[ReviewUnit],
    repo_root: Path,
    run_id: str,
    author_model: str | None = None,
    rejected_category_counts: dict[str, int] | None = None,
    house_rules: str = "",
    sources: dict[str, str] | None = None,
    progress: Callable[[str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    pinned: list[str] | None = None,
    on_findings: Callable[[ReviewUnit, list[Finding], str], None] | None = None,
    output_lang: str = "",
    tiered: bool = False,
) -> tuple[FindingCollection, list[dict], list[dict]]:
    """Orchestrate a full run. Returns (collection, call stats, all static diags).

    Works for quick (1 model) as well as dual/committee (N pinned model versions).
    In dual/committee, findings are clustered and merged across models, then a
    model that did not assert a finding tries to falsify each high-value one.

    ``house_rules`` only reaches the reviewers. The falsifier is deliberately left
    with the stock prompt: a falsifier that has absorbed the project's conventions
    would start agreeing with the reviewer for the wrong reason, which destroys the
    independence the falsification round exists to provide. ``output_lang`` does
    reach the falsifier — it is presentation, not content, and a verdict in a
    language the reader cannot read is worse than no verdict.
    """
    say = progress or (lambda _msg: None)
    active: set[asyncio.Task] = set()
    # Every provider owns a pooled HTTP client. Track them all here so the
    # finally block releases sockets even when a unit or the whole run fails.
    all_providers: list = []

    async def watch_stop() -> None:
        """Cancel every in-flight model call the moment stop is requested."""
        while True:
            if should_stop and should_stop():
                for task in list(active):
                    if not task.done():
                        task.cancel()
                return
            await asyncio.sleep(0.15)

    watcher = asyncio.create_task(watch_stop())
    try:
        scanner_spec: ModelSpec | None = None
        verifier_spec: ModelSpec | None = None
        if mode == "quick" and tiered:
            scanner_spec, verifier_spec = registry.route_quick_tier(
                pinned=pinned, author_model=author_model
            )
            reviewers = [scanner_spec]
            falsifier_spec = None
            if verifier_spec is not None:
                say(
                    t(
                        "orch.tier_start",
                        scanner=scanner_spec.model_id,
                        verifier=verifier_spec.model_id,
                    )
                )
        else:
            reviewers, falsifier_spec = registry.route(
                mode, author_model=author_model, pinned=pinned  # type: ignore[arg-type]
            )
        say(t("orch.start", models=len(reviewers), units=len(units)))

        providers = {m.model_id: _make_provider(m) for m in reviewers}
        all_providers.extend(providers.values())
        review_tasks = [
            asyncio.create_task(
                _review_with(
                    providers[m.model_id],
                    m,
                    units,
                    repo_root,
                    run_id,
                    house_rules,
                    sources,
                    progress=say,
                    on_findings=on_findings,
                    output_lang=output_lang,
                )
            )
            for m in reviewers
        ]
        active.update(review_tasks)
        results = await asyncio.gather(*review_tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                if isinstance(result, asyncio.CancelledError):
                    raise ReviewCancelled(t("server.stopped_by_user")) from result
                raise result
        say(t("orch.merged"))

        per_model_reviews: dict[str, dict[str, list[Finding]]] = {}
        all_rejected: list[dict] = []
        all_calls: list[dict] = []
        all_diags: list[dict] = []
        for m, (per_unit, rej, calls, diags) in zip(reviewers, results, strict=True):
            per_model_reviews[m.model_id] = per_unit
            all_rejected.extend(rej)
            all_calls.extend(calls)
            all_diags.extend(diags)

        if mode == "quick":
            scanner_id = reviewers[0].model_id
            merged = _flatten(per_model_reviews[scanner_id], scanner_id)
            for f in merged:
                f.agreement = "unverified" if not f.verdicts else "unique"

            comparison: dict = {}
            if verifier_spec is not None and merged:
                # Re-review only the units that actually produced a finding. A
                # clean sweep costs no second call; a hit costs one call per unit
                # instead of one per finding, which is the whole time saving.
                hit_units = {f.unit_id for f in merged}
                verify_units = [u for u in units if u.unit_id in hit_units]
                if verify_units:
                    say(
                        t(
                            "orch.verify_round",
                            n=len(verify_units),
                            verifier=verifier_spec.model_id,
                        )
                    )
                    verify_provider = _make_provider(verifier_spec)
                    all_providers.append(verify_provider)
                    verify_gate = asyncio.Semaphore(unit_concurrency())

                    async def verify_one(unit: ReviewUnit):
                        if should_stop and should_stop():
                            raise ReviewCancelled(t("server.stopped_by_user"))
                        async with verify_gate:
                            task = asyncio.create_task(
                                review_unit(
                                    verify_provider,
                                    verifier_spec.model_id,  # type: ignore[union-attr]
                                    unit,
                                    repo_root,
                                    run_id,
                                    house_rules,
                                    sources,
                                    output_lang,
                                )
                            )
                            active.add(task)
                            return await task

                    vresults = await asyncio.gather(
                        *(verify_one(u) for u in verify_units), return_exceptions=True
                    )
                    verified: list[Finding] = []
                    for unit, res in zip(verify_units, vresults, strict=True):
                        if isinstance(res, BaseException):
                            if isinstance(res, asyncio.CancelledError):
                                raise ReviewCancelled(t("server.stopped_by_user")) from res
                            all_calls.append(
                                {
                                    "model": verifier_spec.model_id,
                                    "role": "verify",
                                    "unit_id": unit.unit_id,
                                    "verdict": "error",
                                    "error": str(res),
                                }
                            )
                            say(
                                t(
                                    "orch.verify_failed",
                                    model=verifier_spec.model_id,
                                    file=unit.primary_file,
                                )
                            )
                            continue
                        vf, rej, completion, diags = res
                        all_rejected.extend(rej)
                        all_diags.extend(diags)
                        all_calls.append(
                            {
                                "model": verifier_spec.model_id,
                                "family": verify_provider.family,
                                "unit_id": unit.unit_id,
                                "role": "verify",
                                "latency_ms": completion.latency_ms,
                                "usage": completion.usage.model_dump(),
                            }
                        )
                        verified.extend(vf)
                    merged, extra = _merge_verification(
                        merged, verified, scanner_id, verifier_spec.model_id
                    )
                    comparison = {
                        "mode": "tiered",
                        "scanner": scanner_id,
                        "verifier": verifier_spec.model_id,
                        "verified_units": len(verify_units),
                        "verify_only_findings": extra,
                    }
            merged = rank_findings(merged, rejected_category_counts)
            return (
                FindingCollection(
                    run_id=run_id,
                    repo=str(repo_root),
                    commit="",
                    model_id=scanner_id,
                    spec_level="L1",
                    mode="quick",
                    reviewer_models=[scanner_id],
                    comparison=comparison,
                    findings=merged,
                    rejected=all_rejected,
                ),
                all_calls,
                all_diags,
            )

        # Multi-model: cluster + merge across models.
        per_model_flat = {
            mid: _flatten(per_unit, mid) for mid, per_unit in per_model_reviews.items()
        }
        merged, raw_counts = cluster_findings(per_model_flat)

        # Falsification: a spare model if available, else cross-falsify between reviewers.
        if falsifier_spec is not None:
            falsifier_provider = _make_provider(falsifier_spec)
            all_providers.append(falsifier_provider)
        else:
            falsifier_provider = None

        units_by_id = {u.unit_id: u for u in units}
        targets = [f for f in merged if should_falsify(f)]
        if targets:
            say(t("orch.falsify_round", n=len(targets)))
        # Falsification is another full model call per finding. Sequentially that
        # was another 20–40 minutes on a big run, for calls that only wait on the
        # network — so it runs through the same gate as the review loop.
        falsify_gate = asyncio.Semaphore(unit_concurrency())

        async def falsify_one(f: Finding) -> None:
            if should_stop and should_stop():
                raise ReviewCancelled(t("server.stopped_by_user"))
            source_models = set(f.source_models)
            source_unit = units_by_id.get(f.unit_id)
            context, facts = (
                _context_for(source_unit, repo_root, sources) if source_unit else ("", "")
            )
            # Pick a falsifier that did not assert the finding; prefer the dedicated
            # spare model, otherwise a reviewer outside the finding's source models.
            chosen_provider: Provider | None = None
            chosen_model = ""
            if falsifier_spec is not None and falsifier_spec.model_id not in source_models:
                chosen_provider = falsifier_provider
                chosen_model = falsifier_spec.model_id
            else:
                for m in reviewers:
                    if m.model_id not in source_models:
                        chosen_provider = providers[m.model_id]
                        chosen_model = m.model_id
                        break
            if chosen_provider is None:
                return  # every reviewer reported it and no spare model — leave agreement as-is
            async with falsify_gate:
                try:
                    falsify_task = asyncio.create_task(
                        falsify_finding(
                            chosen_provider, chosen_model, f, context, facts, output_lang
                        )
                    )
                    active.add(falsify_task)
                    verdict, completion = await falsify_task
                    all_calls.append(
                        {
                            "model": chosen_model,
                            "family": chosen_provider.family,
                            "unit_id": f.unit_id,
                            "role": "falsify",
                            "verdict": verdict.verdict,
                            "latency_ms": completion.latency_ms,
                            "usage": completion.usage.model_dump(),
                        }
                    )
                    say(
                        t(
                            "orch.falsify_verdict",
                            model=chosen_model,
                            verdict=verdict.verdict,
                            claim=_one_line(f.claim),
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    all_calls.append(
                        {
                            "model": chosen_model,
                            "role": "falsify",
                            "verdict": "error",
                            "error": str(exc),
                        }
                    )
                    say(
                        t(
                            "orch.falsify_failed",
                            model=chosen_model,
                            claim=_one_line(f.claim),
                        )
                    )

        if targets:
            await asyncio.gather(*(falsify_one(f) for f in targets))

        # Recompute agreement after falsification and drop fully-refuted findings that
        # were only ever asserted by a single model (a unique claim that got broken).
        from .aggregator import agreement as calc_agreement

        surviving: list[Finding] = []
        refuted_findings: list[Finding] = []
        for f in merged:
            f.agreement = calc_agreement(f.verdicts)
            refutes = [v for v in f.verdicts if v.role == "refute"]
            asserts = [v for v in f.verdicts if v.role in ("assert", "confirm")]
            if (
                len(sources_of(f)) <= 1
                and refutes
                and not any(v.role == "confirm" for v in f.verdicts)
            ):
                # single-source claim that the falsifier broke with a counterexample
                all_rejected.append(
                    {
                        "unit_id": f.unit_id,
                        "model": ",".join(f.source_models),
                        "reason": "refuted",
                        "claim": f.claim,
                        "finding_id": f.id,
                        "source_models": list(f.source_models),
                        "verdicts": [v.model_dump(mode="json") for v in f.verdicts],
                    }
                )
                # Keep the full finding for the profile writer: a broken unique
                # claim is exactly the sample that distinguishes a careful model
                # from one that only reports a lot. It must not enter `findings`,
                # whose status column is reserved for human adjudication.
                refuted_findings.append(f)
                continue
            if not asserts and not refutes:
                continue
            surviving.append(f)

        surviving = rank_findings(surviving, rejected_category_counts)

        comparison = {
            "mode": mode,
            "raw_counts": raw_counts,
            "after_clustering": len(merged),
            "after_falsification": len(surviving),
            "zones": {k: len(v) for k, v in _zone_split(surviving).items()},
        }
        # Parse health is a hard gate, so it must outlive the process. Without
        # these artifacts a run that quietly lost findings is indistinguishable
        # from one where the model genuinely found nothing.
        parse_stats: dict[str, int | float | None] = {}
        parse_failures: list[dict] = []
        for provider in all_providers:
            for key, value in getattr(provider, "parse_stats", {}).items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    parse_stats[key] = parse_stats.get(key, 0) + value
            for failure in getattr(provider, "failure_log", []):
                parse_failures.append(
                    failure.model_dump(mode="json")
                    if hasattr(failure, "model_dump")
                    else dict(failure)
                )
        parse_calls = parse_stats.get("calls", 0)
        parse_stats["success_rate"] = (
            round(
                (parse_stats.get("first_try", 0) + parse_stats.get("repaired", 0)) / parse_calls,
                3,
            )
            if parse_calls
            else None
        )
        comparison["parse"] = parse_stats
        comparison["parse_failures"] = parse_failures[:50]

        collection = FindingCollection(
            run_id=run_id,
            repo=str(repo_root),
            commit="",
            model_id=",".join(m.model_id for m in reviewers),
            spec_level="L1",
            mode=mode,
            reviewer_models=[m.model_id for m in reviewers],
            falsifier_model=falsifier_spec.model_id if falsifier_spec else None,
            comparison=comparison,
            findings=surviving,
            refuted_findings=refuted_findings,
            rejected=all_rejected,
        )
        return collection, all_calls, all_diags
    except asyncio.CancelledError as exc:
        if should_stop and should_stop():
            raise ReviewCancelled(t("server.stopped_by_user")) from exc
        raise
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        for provider in all_providers:
            close = getattr(provider, "aclose", None)
            if close is not None:
                await close()


def sources_of(f: Finding) -> list[str]:
    return f.source_models or [v.model_id for v in f.verdicts if v.role == "assert"]


def _zone_split(findings: list[Finding]) -> dict[str, list[Finding]]:
    zones: dict[str, list[Finding]] = {"consensus": [], "unique": [], "conflict": []}
    for f in findings:
        if f.agreement == "conflict":
            zones["conflict"].append(f)
        elif f.agreement in ("unanimous", "majority"):
            zones["consensus"].append(f)
        else:
            zones["unique"].append(f)
    return zones
