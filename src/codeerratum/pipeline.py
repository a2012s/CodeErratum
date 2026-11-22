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
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .config import Config
from .config_store import CONFIG_REL
from .config_store import load as load_saved_config
from .ingest import DiffError, get_commit, get_diff, new_side_sources, split_diff
from .models import FindingCollection, ModelRegistry, ReviewUnit
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
        return {
            "run_id": self.run_id,
            "created_at": self.created_at,
            "mode": self.collection.mode,
            "commit": self.collection.commit,
            "models": self.collection.reviewer_models,
            "falsifier": self.collection.falsifier_model,
            "findings": len(self.collection.findings),
            "rejected": len(self.collection.rejected),
            "units_total": self.units_total,
            "units_selected": self.units_selected,
            "calls": len(self.calls),
            "static_diags": len(self.diags),
            "latency_ms_avg": round(self.latency_ms_avg, 1),
            "tokens": self.token_totals(),
            "zones": {k: len(v) for k, v in self.collection.zoned().items()},
            "comparison": self.collection.comparison,
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
    roles = [verdict.role for f in findings for verdict in f.verdicts]
    if not roles:
        return ""
    abstained = sum(1 for role in roles if role == "abstain")
    if abstained / len(roles) <= 0.3:
        return ""
    return (
        f"证伪轮 {abstained}/{len(roles)} 判为证据不足（>30%），上下文构建可能不足，结论请谨慎采用"
    )


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
    max_units: int = 10,
    author_model: str | None = None,
    out: Path | None = None,
    registry: ModelRegistry | None = None,
    progress: Callable[[str], None] | None = None,
    house_rules: str = "",
    paths: str = "",
    should_stop: Callable[[], bool] | None = None,
) -> ReviewOutcome:
    """Run the whole pipeline and persist artifacts + feedback rows.

    ``out`` writes an extra copy of the work order (the CLI's ``-o``). The run
    directory always keeps its own ``review.md`` so the UI can serve a run
    without touching the working tree. ``paths`` narrows the diff to a git
    pathspec (e.g. ``src/api``), so a review can target one folder without
    staging changes first. ``should_stop`` is polled by the orchestrator; when
    it turns true, in-flight model calls are cancelled and ReviewCancelled is
    raised instead of persisting a partial report.
    """
    emit = progress or (lambda _msg: None)

    def say(message: str) -> None:
        # Check at every lifecycle message too, not just inside model calls —
        # diff splitting / report writing have no other cancellation point.
        if should_stop and should_stop():
            raise ReviewCancelled("审查已被停止")
        emit(message)

    repo_root = repo_root.resolve()
    if not (repo_root / ".git").exists():
        raise NotARepository(f"{repo_root} is not a git repository")

    # A saved config (written by the UI) wins over env so an edit sticks across
    # process restarts. A corrupt file is an error, never a silent fallback —
    # otherwise the user thinks the old model set is still in force.
    try:
        config = load_saved_config(repo_root)
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read {CONFIG_REL}: {exc}") from exc
    if config is None:
        try:
            config = Config.from_env()
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
    if registry is not None:
        config = Config(
            base_url=config.base_url,
            api_key=config.api_key,
            model=config.model,
            registry=registry,
            mode=config.mode,
        )
    run_mode = resolve_mode(mode, config)

    run_spec = diff_spec.strip() or "HEAD"
    try:
        diff_text = get_diff(run_spec, repo_root, paths=paths)
    except DiffError as exc:
        raise DiffFailed(str(exc)) from exc
    if not diff_text.strip():
        raise NoChanges("No changes in diff. Nothing to review.")

    run_id = uuid.uuid4().hex[:12]
    run_dir = repo_root / STATE_DIR / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "input.diff").write_text(diff_text, encoding="utf-8")

    notices: list[str] = []
    if paths.strip():
        notices.append(f"仅审查路径 {paths.strip()} 内的改动（其他目录未审）")
        say(notices[-1])
    rules = resolve_house_rules(house_rules, repo_root)
    if rules.truncated:
        notices.append(f"项目规范超过 {MAX_HOUSE_RULES_CHARS} 字，已截断")
        say(notices[-1])
    if rules:
        say(f"Project rules: {rules.source} ({rules.chars} chars, sha {rules.sha})")
    # Read the new side from the diff revision, not the working tree: reviewing
    # main...feature while on main used to slice feature's line numbers into
    # main's text, and the anchor hash happily confirmed the wrong region.
    try:
        sources = new_side_sources(run_spec, repo_root, paths=paths)
    except DiffError as exc:
        sources = {}
        notices.append(f"无法读取 diff 版本的文件内容（{exc}），锚点改为基于工作区校验")
        say(notices[-1])
    units = split_diff(diff_text, repo_root, notices=notices, sources=sources)
    (run_dir / "units.json").write_text(
        json.dumps([u.model_dump() for u in units], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    say(f"Split diff into {len(units)} review unit(s)")
    for note in notices:
        say(note)
    if not units:
        raise NoChanges("No reviewable units found.")

    selected = prioritize(units)[:max_units]
    if len(units) > max_units:
        capped = f"仅审查 {max_units}/{len(units)} 个单元（--max-units 可调整）"
        notices.append(capped)
        say(capped)

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
            )
        )
        say("生成报告并写入存储")
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc

    failed = [r for r in collection.rejected if r.get("reason") == "provider_error"]
    if failed:
        notices.append(f"{len(failed)} 个单元调用模型失败，相关文件未被审查")

    # A high abstain rate is a signal about *our* context, not about the code: the
    # falsifier keeps saying it cannot decide. Surfacing it turns a silent
    # degradation into a number the user can act on.
    warning = abstain_warning(collection.findings)
    if warning:
        notices.append(warning)

    collection.notes = notices
    collection.commit = get_commit(repo_root)
    collection.spec_level = spec_level
    collection.spec_level_reason = spec_reason
    collection.house_rules_sha = rules.sha
    collection.house_rules_chars = rules.chars
    collection.house_rules_source = rules.source

    (run_dir / "calls.json").write_text(
        json.dumps(calls, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (run_dir / "findings.json").write_text(
        json.dumps(collection.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (run_dir / "static_diags.json").write_text(
        json.dumps(all_diags, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    ts = datetime.now(UTC).isoformat(timespec="seconds")
    markdown = render_markdown(collection, ts)
    (run_dir / "review.md").write_text(markdown, encoding="utf-8")
    out_path: Path | None = None
    if out is not None:
        out_path = out if out.is_absolute() else repo_root / out
        out_path.write_text(markdown, encoding="utf-8")

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
