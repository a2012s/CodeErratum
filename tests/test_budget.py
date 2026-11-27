"""The token fuse: a run that runs out of money stops like a stopped run.

``max_units`` caps how much work a sitting takes on; it says nothing about how
many tokens that work costs. A user on a token plan wants both: a number written
down *before* the first paid call, and a hard stop that keeps what was already
bought instead of losing it.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from codeerratum import pipeline
from codeerratum.cli import app
from codeerratum.models import FindingCollection, ModelSpec
from codeerratum.models.finding import (
    Anchor,
    DefectCategory,
    Finding,
    Lens,
    Suggestion,
    snippet_hash,
)
from codeerratum.models.unit import Hunk, ReviewUnit
from codeerratum.report.markdown import render_markdown
from codeerratum.review import orchestrator as orch

# ---------------------------------------------------------------- helpers


def _usage(inp: int, out: int) -> dict:
    return {"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out}


class _Registry:
    def route(self, mode, author_model=None, pinned=None):
        return [ModelSpec(model_id="m", base_url="http://x")], None

    def route_quick_tier(self, pinned=None, author_model=None):
        return ModelSpec(model_id="m", base_url="http://x"), None


def _finding(index: int) -> Finding:
    snippet = f"x{index} = 1\n"
    return Finding(
        id=f"{index:016d}",
        unit_id=f"u{index}",
        lens=Lens.CORRECTNESS,
        category=DefectCategory.BOUNDARY,
        severity="P2",
        claim=f"问题{index}",
        detail="d",
        anchors=[
            Anchor(
                file="a.py",
                start_line=index,
                end_line=index,
                snippet=snippet,
                snippet_hash=snippet_hash(snippet),
            )
        ],
        agreement="unique",
        source_models=["m"],
        suggestion=Suggestion(diff=None, explanation="e", acceptance="pytest -q"),
    )


def _paying_review_with(
    billed_units: int,
    spend: tuple[int, int],
    complete: bool = True,
    hang: float = 5.0,
):
    """A ``_review_with`` double that bills `spend` per unit, then stalls.

    The stall is the point: the fuse is polled, not signalled, so the double has
    to still be in flight when ``watch_stop`` looks at the ledger.

    ``complete=False`` records spend without coverage, which is what the verify
    and falsify phases do: they mirror the call into ``partial.calls`` but never
    touch ``partial.per_unit``.
    """

    async def fake(
        provider,
        spec,
        units,
        repo_root,
        run_id,
        house_rules="",
        sources=None,
        progress=None,
        on_findings=None,
        output_lang="",
        sink=None,
        on_unit_complete=None,
        cache=None,
    ):
        assert sink is not None
        per_unit: dict[str, list[Finding]] = {}
        for index, unit in enumerate(units[:billed_units], start=1):
            sink.calls.append(
                {
                    "model": spec.model_id,
                    "unit_id": unit.unit_id,
                    "usage": _usage(*spend),
                    "cached": False,
                }
            )
            if complete:
                finding = _finding(index)
                per_unit[unit.unit_id] = [finding]
                sink.per_unit.setdefault(spec.model_id, {})[unit.unit_id] = [finding]
                sink.units_done.add(unit.unit_id)
        if on_unit_complete:
            on_unit_complete()
        await asyncio.sleep(hang)
        raise AssertionError("the fuse must have cancelled this call")

    return fake


# ---------------------------------------------------------------- env fallback


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, None),
        ("", None),
        ("0", None),
        ("-5", None),
        ("abc", None),
        (" 45 ", 45),
        ("123", 123),
    ],
)
def test_env_budget_only_accepts_a_positive_int(monkeypatch, raw, expected):
    """A typo in the environment must not silently review nothing (or everything)."""
    monkeypatch.delenv(pipeline.TOKEN_BUDGET_ENV, raising=False)
    if raw is not None:
        monkeypatch.setenv(pipeline.TOKEN_BUDGET_ENV, raw)
    assert pipeline.token_budget_from_env() == expected


# ---------------------------------------------------------------- the fuse


def test_fuse_stops_the_run_and_keeps_the_units_it_paid_for(monkeypatch, tmp_path):
    """Crossing the budget is a stop, not a failure: partial report, budget recorded."""
    monkeypatch.setattr(orch, "_make_provider", lambda _spec: object())
    monkeypatch.setattr(orch, "_review_with", _paying_review_with(1, (60, 40)))

    collection, _calls, _diags = asyncio.run(
        orch.run_review(
            _Registry(),
            "quick",
            [_mk_unit(i) for i in (1, 2, 3)],
            tmp_path,
            "run",
            token_budget=50,
        )
    )

    assert collection.stopped is True
    budget = collection.comparison["budget"]
    assert budget["token_budget"] == 50
    assert budget["spent"] == 100
    assert collection.comparison["stopped"]["units_completed"] == 1


def test_spend_without_a_completed_unit_still_blames_the_budget(monkeypatch, tmp_path):
    """Nothing coverable left: the caller must hear "out of money", not "you stopped it".

    A cancelled review task surfaces as ReviewCancelled("stopped by user"), so the
    fuse flag has to be consulted before the exception type, or a machine stop reads
    as a human decision in the CLI error and the run list.
    """
    monkeypatch.setattr(orch, "_make_provider", lambda _spec: object())
    monkeypatch.setattr(orch, "_review_with", _paying_review_with(1, (60, 40), complete=False))

    with pytest.raises(orch.ReviewCancelled) as excinfo:
        asyncio.run(
            orch.run_review(
                _Registry(),
                "quick",
                [_mk_unit(1)],
                tmp_path,
                "run",
                token_budget=10,
            )
        )

    message = str(excinfo.value)
    assert "10" in message and "100" in message
    assert "熔断" in message or "budget" in message.lower()


def test_a_replayed_unit_does_not_burn_the_budget(monkeypatch, tmp_path):
    """Cache hits carry zero usage, so reuse must not trip a fuse it did not pay for."""
    monkeypatch.setattr(orch, "_make_provider", lambda _spec: object())

    async def replaying(
        provider,
        spec,
        units,
        repo_root,
        run_id,
        house_rules="",
        sources=None,
        progress=None,
        on_findings=None,
        output_lang="",
        sink=None,
        on_unit_complete=None,
        cache=None,
    ):
        for unit in units:
            sink.calls.append(
                {"model": spec.model_id, "unit_id": unit.unit_id, "usage": _usage(0, 0)}
            )
            sink.units_done.add(unit.unit_id)
        return {}, list(sink.calls), [], []

    async def fake_review(*args, **kwargs):
        return await replaying(*args, **kwargs)

    monkeypatch.setattr(orch, "_review_with", fake_review)

    collection, _calls, _diags = asyncio.run(
        orch.run_review(
            _Registry(),
            "quick",
            [_mk_unit(1), _mk_unit(2)],
            tmp_path,
            "run",
            token_budget=10,
        )
    )
    assert collection.stopped is not True
    assert "budget" not in collection.comparison


def test_no_budget_means_no_fuse(monkeypatch, tmp_path):
    """Without a budget the watcher must never touch the ledger, however it fills up."""
    monkeypatch.setattr(orch, "_make_provider", lambda _spec: object())

    async def spending(
        provider,
        spec,
        units,
        repo_root,
        run_id,
        house_rules="",
        sources=None,
        progress=None,
        on_findings=None,
        output_lang="",
        sink=None,
        on_unit_complete=None,
        cache=None,
    ):
        for unit in units:
            sink.calls.append(
                {"model": spec.model_id, "unit_id": unit.unit_id, "usage": _usage(900, 900)}
            )
            sink.units_done.add(unit.unit_id)
        return {}, list(sink.calls), [], []

    monkeypatch.setattr(orch, "_review_with", spending)

    collection, _calls, _diags = asyncio.run(
        orch.run_review(_Registry(), "quick", [_mk_unit(1)], tmp_path, "run")
    )
    assert collection.stopped is not True
    assert "budget" not in collection.comparison


def _mk_unit(index: int):
    return ReviewUnit(
        unit_id=f"u{index}",
        primary_file="a.py",
        symbol=f"f{index}",
        kind="function",
        hunks=[Hunk(old_start=1, old_lines=2, new_start=index, new_lines=2, content="+x")],
        changed_lines=1,
    )


# ---------------------------------------------------------------- pipeline


def _repo(root: Path, files: int = 3) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.io"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    for i in range(files):
        (root / f"mod_{i}.py").write_text(f"def f_{i}():\n    return {i}\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
    for i in range(files):
        (root / f"mod_{i}.py").write_text(f"def f_{i}():\n    return {i} + 1\n", encoding="utf-8")
    return root


def _stub_run_review(monkeypatch, seen: dict) -> None:
    monkeypatch.setenv("CODEERRATUM_BASE_URL", "http://stub.invalid/v1")
    monkeypatch.setenv("CODEERRATUM_MODEL", "gpt-stub")
    monkeypatch.delenv("CODEERRATUM_MODELS", raising=False)

    async def fake_run(registry, mode, units, repo_root, run_id, **kwargs):
        seen["kwargs"] = kwargs
        seen["units"] = len(units)
        return (
            FindingCollection(
                run_id=run_id,
                repo=str(repo_root),
                commit="",
                model_id="m",
                spec_level="L1",
            ),
            [],
            [],
        )

    monkeypatch.setattr(pipeline, "run_review", fake_run)


def test_estimate_is_written_before_anything_is_paid(monkeypatch, tmp_path):
    """The number has to exist even if the first call never returns."""
    repo = _repo(tmp_path / "repo")
    seen: dict = {}
    _stub_run_review(monkeypatch, seen)
    notes: list[str] = []
    pipeline.execute_review(repo, diff_spec="HEAD", progress=notes.append)

    estimates = list((repo / pipeline.STATE_DIR / "runs").glob("*/estimate.json"))
    assert len(estimates) == 1
    data = json.loads(estimates[0].read_text(encoding="utf-8"))
    assert data["units"] == seen["units"] >= 1
    assert data["seats"] >= 1
    assert data["est_input_tokens"] >= 1
    assert data["token_budget"] is None
    assert any("预估" in n or "Cost preview" in n for n in notes)


def test_budget_reaches_the_orchestrator_and_warns_when_too_small(monkeypatch, tmp_path):
    """A fuse smaller than the estimate is worth saying out loud, before the spend."""
    repo = _repo(tmp_path / "repo")
    seen: dict = {}
    _stub_run_review(monkeypatch, seen)
    notes: list[str] = []
    pipeline.execute_review(repo, diff_spec="HEAD", progress=notes.append, token_budget=1)

    assert seen["kwargs"]["token_budget"] == 1
    assert any("预算" in n or "budget" in n.lower() for n in notes)


def test_env_budget_is_the_fallback_not_the_override(monkeypatch, tmp_path):
    """The flag wins; the environment only fills in when no flag was given."""
    repo = _repo(tmp_path / "repo")
    monkeypatch.setenv(pipeline.TOKEN_BUDGET_ENV, "77")
    seen: dict = {}
    _stub_run_review(monkeypatch, seen)
    pipeline.execute_review(repo, diff_spec="HEAD")
    assert seen["kwargs"]["token_budget"] == 77

    seen = {}
    _stub_run_review(monkeypatch, seen)
    pipeline.execute_review(repo, diff_spec="HEAD", token_budget=5)
    assert seen["kwargs"]["token_budget"] == 5

    # An explicit 0 is the documented "no cap" from MCP/Web. With `or` it was
    # collapsed into the env fallback and silently re-armed a disabled budget.
    seen = {}
    _stub_run_review(monkeypatch, seen)
    pipeline.execute_review(repo, diff_spec="HEAD", token_budget=0)
    assert seen["kwargs"]["token_budget"] == 0


def test_a_fused_run_records_the_reason_in_the_work_order(monkeypatch, tmp_path):
    """The report is the handoff: it must not read like the reviewer quit on its own."""
    repo = _repo(tmp_path / "repo")
    monkeypatch.setenv("CODEERRATUM_BASE_URL", "http://stub.invalid/v1")
    monkeypatch.setenv("CODEERRATUM_MODEL", "gpt-stub")
    monkeypatch.delenv("CODEERRATUM_MODELS", raising=False)

    async def fused(registry, mode, units, repo_root, run_id, **kwargs):
        collection = FindingCollection(
            run_id=run_id,
            repo=str(repo_root),
            commit="",
            model_id="m",
            spec_level="L1",
            stopped=True,
        )
        collection.comparison = {
            "stopped": {"stage": "review", "units_completed": 1, "units_total": 9},
            "budget": {"token_budget": 10, "spent": 42},
        }
        return collection, [], []

    monkeypatch.setattr(pipeline, "run_review", fused)
    notes: list[str] = []
    outcome = pipeline.execute_review(
        repo, diff_spec="HEAD", progress=notes.append, out=repo / "report.md"
    )

    # Locale-agnostic: the note carries the spend/budget pair in both languages.
    assert any("42/10" in n for n in notes)
    text = (repo / "report.md").read_text(encoding="utf-8")
    assert "token 预算熔断" in text
    # The fuse line must stay inside the stopped-run quote block. A bare line
    # closed the block, so the execution AI read the cause as outside the banner.
    cause_line = next(line for line in text.splitlines() if "token 预算熔断" in line)
    assert cause_line.startswith("> "), f"cause escaped the quote block: {cause_line!r}"
    assert outcome.out_path is not None


def test_cli_exposes_the_fuse_on_both_entry_points():
    """Ask the command object, not the rendered help text (terminal width lies)."""
    from typer.main import get_command

    commands = get_command(app).commands
    for name in ("review", "resume"):
        params = commands[name].params
        assert any("--token-budget" in p.opts for p in params), name


def test_banner_names_the_fuse_and_points_at_resume():
    collection = FindingCollection(
        run_id="r1",
        repo="/x",
        commit="",
        model_id="m",
        spec_level="L1",
        stopped=True,
    )
    collection.comparison = {
        "stopped": {"stage": "review", "units_completed": 2, "units_total": 30},
        "budget": {"token_budget": 100, "spent": 140},
    }
    text = render_markdown(collection, "2026-11-28 10:00")
    assert "token 预算熔断" in text
    assert "140/100" in text
    assert "resume" in text
