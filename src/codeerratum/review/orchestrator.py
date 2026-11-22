"""Phase-3 orchestrator — heterogeneous parallel review + aggregation + falsification.

The outer structure stays a fixed, reproducible pipeline (this is an agentic
workflow, not an autonomous agent): all models review in parallel, findings are
clustered, then high-value findings are falsified by a different-vendor model.

Backward compatible: mode='quick' delegates to the single-model review_unit path.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

from ..context import build_context
from ..evidence import collect_static, known_facts_block
from ..models import Finding, FindingCollection, ModelRegistry, ModelSpec, ReviewUnit
from ..providers import OpenAICompatProvider, Provider
from .aggregator import cluster_findings, rank_findings
from .executor import review_unit
from .falsification import falsify_finding, should_falsify


class ReviewCancelled(Exception):
    """The user stopped the run. Distinct from a failure: no error dialog, no report."""


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
    context = "\n".join([ctx.get("enclosing", ""), *ctx.get("callers", [])])
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
) -> tuple[dict[str, list[Finding]], list[dict], list[dict], list[dict]]:
    """Run one model over all units. Returns its findings + rejected + call stats + diags."""
    say = progress or (lambda _msg: None)
    per_unit: dict[str, list[Finding]] = {}
    rejected: list[dict] = []
    calls: list[dict] = []
    diags_all: list[dict] = []
    total = len(units)
    for idx, unit in enumerate(units, 1):
        say(f"[{spec.model_id}] 审查单元 {idx}/{total} · {unit.primary_file}")
        try:
            findings, rej, completion, diags = await review_unit(
                provider, spec.model_id, unit, repo_root, run_id, house_rules, sources
            )
        except Exception as exc:  # noqa: BLE001 - one model/unit failing must not kill the run
            rejected.append(
                {
                    "unit_id": unit.unit_id,
                    "model": spec.model_id,
                    "reason": "provider_error",
                    "error": str(exc),
                }
            )
            say(f"[{spec.model_id}] 单元 {idx}/{total} 调用失败：{type(exc).__name__}")
            continue
        per_unit.setdefault(unit.unit_id, []).extend(findings)
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
        say(f"[{spec.model_id}] 单元 {idx}/{total} 完成（{len(findings)} 条发现）")
    return per_unit, rejected, calls, diags_all


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
) -> tuple[FindingCollection, list[dict], list[dict]]:
    """Orchestrate a full run. Returns (collection, call stats, all static diags).

    Works for quick (1 model) as well as dual/committee (N heterogeneous models).
    In dual/committee, findings are clustered and merged across models, then a
    different-vendor model tries to falsify each high-value finding.

    ``house_rules`` only reaches the reviewers. The falsifier is deliberately left
    with the stock prompt: a falsifier that has absorbed the project's conventions
    would start agreeing with the reviewer for the wrong reason, which destroys the
    independence the falsification round exists to provide.
    """
    say = progress or (lambda _msg: None)
    active: set[asyncio.Task] = set()

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
        reviewers, falsifier_spec = registry.route(mode, author_model=author_model)  # type: ignore[arg-type]
        say(f"启动 {len(reviewers)} 个审查模型并行评审 {len(units)} 个单元")

        providers = {m.model_id: _make_provider(m) for m in reviewers}
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
                )
            )
            for m in reviewers
        ]
        active.update(review_tasks)
        results = await asyncio.gather(*review_tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                if isinstance(result, asyncio.CancelledError):
                    raise ReviewCancelled("审查已被用户停止") from result
                raise result
        say("所有模型评审完成，开始聚类合并")

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
            merged = _flatten(per_model_reviews[reviewers[0].model_id], reviewers[0].model_id)
            for f in merged:
                f.agreement = "unverified" if not f.verdicts else "unique"
            merged = rank_findings(merged, rejected_category_counts)
            return (
                FindingCollection(
                    run_id=run_id,
                    repo=str(repo_root),
                    commit="",
                    model_id=reviewers[0].model_id,
                    spec_level="L1",
                    mode="quick",
                    reviewer_models=[reviewers[0].model_id],
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

        # Falsification: a third vendor if available, else cross-falsify between reviewers.
        if falsifier_spec is not None:
            falsifier_provider = _make_provider(falsifier_spec)
        else:
            falsifier_provider = None

        units_by_id = {u.unit_id: u for u in units}
        targets = [f for f in merged if should_falsify(f)]
        if targets:
            say(f"证伪轮：{len(targets)} 条高价值发现交叉验证")
        for f in targets:
            if should_stop and should_stop():
                raise ReviewCancelled("审查已被用户停止")
            source_models = set(f.source_models)
            source_unit = units_by_id.get(f.unit_id)
            context, facts = (
                _context_for(source_unit, repo_root, sources) if source_unit else ("", "")
            )
            # Pick a falsifier from a family not among the sources; prefer the dedicated
            # third vendor, otherwise the reviewer with a different family than source.
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
                continue  # every reviewer reported it and no third vendor — leave agreement as-is
            try:
                falsify_task = asyncio.create_task(
                    falsify_finding(chosen_provider, chosen_model, f, context, facts)
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
                say(f"证伪 {chosen_model}：{verdict.verdict} · {f.claim[:40]}")
            except Exception as exc:  # noqa: BLE001
                all_calls.append(
                    {
                        "model": chosen_model,
                        "role": "falsify",
                        "verdict": "error",
                        "error": str(exc),
                    }
                )
                say(f"证伪 {chosen_model}：调用失败 · {f.claim[:40]}")

        # Recompute agreement after falsification and drop fully-refuted findings that
        # were only ever asserted by a single model (a unique claim that got broken).
        from .aggregator import agreement as calc_agreement

        surviving: list[Finding] = []
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
                    }
                )
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
            rejected=all_rejected,
        )
        return collection, all_calls, all_diags
    except asyncio.CancelledError as exc:
        if should_stop and should_stop():
            raise ReviewCancelled("审查已被用户停止") from exc
        raise
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


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
