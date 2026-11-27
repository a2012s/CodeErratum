"""Crash checkpoints — a killed process must not throw away the units it paid for.

The run artifacts double as a ledger: ``resume`` reads findings.json + calls.json
and skips the units already covered. Before 0.7.4 that ledger appeared only after
the final unit returned, so a process killed at 95% lost 95% of the run.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

import codeerratum.review.orchestrator as orch
from codeerratum import pipeline
from codeerratum.models import ModelSpec
from codeerratum.models.finding import (
    Anchor,
    DefectCategory,
    Finding,
    Lens,
    Suggestion,
    snippet_hash,
)
from codeerratum.models.unit import Hunk, ReviewUnit
from codeerratum.resume import plan_resume


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


def _unit(index: int) -> ReviewUnit:
    return ReviewUnit(
        unit_id=f"u{index}",
        primary_file="a.py",
        symbol=f"f{index}",
        kind="function",
        hunks=[Hunk(old_start=1, old_lines=2, new_start=index, new_lines=2, content="+x")],
        changed_lines=1,
    )


class _Registry:
    def route(self, mode, author_model=None, pinned=None):
        return [ModelSpec(model_id="m", base_url="http://x")], None

    def route_quick_tier(self, pinned=None, author_model=None):
        return ModelSpec(model_id="m", base_url="http://x"), None


def _mirroring_review_with(done: int, then: BaseException | None = None):
    """A ``_review_with`` double: fill the sink for `done` units, then do `then`."""

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
    ):
        assert sink is not None
        per_unit: dict = {}
        for index, unit in enumerate(units[:done], start=1):
            finding = _finding(index)
            per_unit[unit.unit_id] = [finding]
            sink.per_unit.setdefault(spec.model_id, {})[unit.unit_id] = [finding]
            sink.calls.append({"model": spec.model_id, "unit_id": unit.unit_id, "usage": {}})
            sink.units_done.add(unit.unit_id)
            if on_unit_complete:
                on_unit_complete()
        if then is not None:
            raise then
        return per_unit, [], list(sink.calls), []

    return fake


def test_run_review_fires_checkpoints_per_unit(monkeypatch, tmp_path):
    """Reverting the on_unit_complete wiring makes a crash lose the whole run."""
    monkeypatch.setenv("CODEERRATUM_CHECKPOINT_UNITS", "1")
    monkeypatch.setenv("CODEERRATUM_CHECKPOINT_SECONDS", "0.001")
    monkeypatch.setattr(orch, "_make_provider", lambda _spec: object())
    monkeypatch.setattr(
        orch, "_review_with", _mirroring_review_with(2, then=asyncio.CancelledError())
    )

    seen: list = []
    asyncio.run(
        orch.run_review(
            _Registry(),
            "quick",
            [_unit(1), _unit(2)],
            tmp_path,
            "run",
            on_checkpoint=lambda coll, calls, diags: seen.append((coll, calls, diags)),
        )
    )
    assert len(seen) >= 2, "every completed unit must be able to trigger a flush"
    for coll, _calls, _diags in seen:
        assert coll.stopped is True
    assert [c["unit_id"] for c in seen[-1][1]] == ["u1", "u2"]


def test_checkpoint_throttle_stays_shut_below_both_bounds(monkeypatch, tmp_path):
    """One finished unit out of a 60-unit budget is not worth a re-serialise."""
    monkeypatch.setenv("CODEERRATUM_CHECKPOINT_UNITS", "99")
    monkeypatch.setenv("CODEERRATUM_CHECKPOINT_SECONDS", "3600")
    monkeypatch.setattr(orch, "_make_provider", lambda _spec: object())
    monkeypatch.setattr(orch, "_review_with", _mirroring_review_with(1))

    seen: list = []
    asyncio.run(
        orch.run_review(
            _Registry(),
            "quick",
            [_unit(1)],
            tmp_path,
            "run",
            on_checkpoint=lambda *a: seen.append(a),
        )
    )
    # The per-unit throttle stayed shut; the forced flush after the review gather
    # is what made the ledger durable before falsification spends anything more.
    assert len(seen) == 1
    assert seen[0][0].comparison["stopped"]["units_completed"] == 1


def test_a_broken_checkpoint_writer_never_kills_a_run(monkeypatch, tmp_path):
    """A full disk mid-run must not abandon the tokens already spent."""
    monkeypatch.setenv("CODEERRATUM_CHECKPOINT_UNITS", "1")
    monkeypatch.setenv("CODEERRATUM_CHECKPOINT_SECONDS", "0.001")
    monkeypatch.setattr(orch, "_make_provider", lambda _spec: object())
    monkeypatch.setattr(orch, "_review_with", _mirroring_review_with(1))

    def boom(*_args):
        raise OSError("disk full")

    collection, _calls, _diags = asyncio.run(
        orch.run_review(_Registry(), "quick", [_unit(1)], tmp_path, "run", on_checkpoint=boom)
    )
    assert [f.id for f in collection.findings] == [_finding(1).id]


def test_no_checkpoint_before_the_first_unit(monkeypatch, tmp_path):
    """An empty sink means nothing is durable yet; do not write an empty run."""
    monkeypatch.setenv("CODEERRATUM_CHECKPOINT_UNITS", "1")
    monkeypatch.setattr(orch, "_make_provider", lambda _spec: object())

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
    ):
        if on_unit_complete:
            on_unit_complete()  # fires before anything landed in the sink
        return {}, [], [], []

    monkeypatch.setattr(orch, "_review_with", fake)
    seen: list = []
    asyncio.run(
        orch.run_review(
            _Registry(),
            "quick",
            [_unit(1)],
            tmp_path,
            "run",
            on_checkpoint=lambda *a: seen.append(a),
        )
    )
    assert seen == []


def test_write_run_ledger_keeps_calls_json_last(tmp_path, monkeypatch):
    """The ordering invariant: findings first, coverage ledger last.

    A crash between the two must leave calls.json *behind*, so the next resume
    re-reviews those units (costs tokens) instead of treating units as covered
    whose findings were never written (loses data).
    """
    from codeerratum.models import FindingCollection

    coll = FindingCollection(
        run_id="r1", repo=".", commit="", model_id="m", spec_level="L1", findings=[_finding(1)]
    )

    def boom(*_args, **_kwargs):
        raise OSError("cannot render")

    monkeypatch.setattr(pipeline, "render_markdown", boom)
    with pytest.raises(OSError):
        pipeline.write_run_ledger(tmp_path, coll, [{"model": "m", "unit_id": "u1"}], [], "t")
    assert (tmp_path / "findings.json").exists()
    assert not (tmp_path / "calls.json").exists()

    monkeypatch.setattr(pipeline, "render_markdown", lambda *_a, **_k: "# ok")
    pipeline.write_run_ledger(tmp_path, coll, [{"model": "m", "unit_id": "u1"}], [], "t")
    assert json.loads((tmp_path / "calls.json").read_text(encoding="utf-8"))[0]["unit_id"] == "u1"


# ---- the pipeline-level story: kill the process, resume the run -------------


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.io"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "a.py").write_text(
        "".join(f"def f{i}():\n    return {i}\n" for i in range(6)), encoding="utf-8"
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    (tmp_path / "a.py").write_text(
        "".join(f"def f{i}():\n    x = []\n    return x[{i}]\n" for i in range(6)),
        encoding="utf-8",
    )
    return tmp_path


def test_a_killed_process_leaves_a_resumable_ledger(repo, monkeypatch):
    """The whole point: die mid-review, then plan_resume picks up where it stopped."""
    monkeypatch.setenv("CODEERRATUM_BASE_URL", "http://stub.invalid/v1")
    monkeypatch.setenv("CODEERRATUM_MODEL", "m")
    monkeypatch.delenv("CODEERRATUM_MODELS", raising=False)
    monkeypatch.setenv("CODEERRATUM_CHECKPOINT_UNITS", "1")
    monkeypatch.setenv("CODEERRATUM_CHECKPOINT_SECONDS", "0.001")
    monkeypatch.setattr(orch, "_make_provider", lambda _spec: object())
    monkeypatch.setattr(orch, "_review_with", _mirroring_review_with(2, then=SystemExit(137)))

    with pytest.raises(SystemExit):
        pipeline.execute_review(repo, "HEAD", mode="quick", max_units=10)

    runs = sorted((repo / ".codeerratum" / "runs").iterdir())
    assert len(runs) == 1, "the checkpointed run directory must survive the crash"
    run_dir: Path = runs[0]
    queue = json.loads((run_dir / "units.json").read_text(encoding="utf-8"))
    assert len(queue) > 2, "the fixture must leave units unreviewed, or there is no crash story"
    done_ids = [u["unit_id"] for u in queue[:2]]
    ledger = json.loads((run_dir / "calls.json").read_text(encoding="utf-8"))
    assert [c["unit_id"] for c in ledger] == done_ids
    dump = json.loads((run_dir / "findings.json").read_text(encoding="utf-8"))
    assert dump["stopped"] is True
    assert (run_dir / "review.md").exists()
    assert (run_dir / "static_diags.json").exists()

    plan = plan_resume(repo, run_dir.name)
    assert plan["units_done"] == len(done_ids)
    assert plan["units_remaining_all"] == len(queue) - 2
    assert set(plan["batch_unit_ids"]) == {u["unit_id"] for u in queue} - set(done_ids)


def test_a_crash_before_the_first_unit_leaves_no_run(repo, monkeypatch):
    """No completed unit means no ledger, so the run is neither listed nor resumable.

    A crash cannot be caught, so the scratch files written before the first seat
    starts (input.diff / run_meta.json / units.json) do stay on disk. What matters
    is that the two history sources both refuse to show it: the run list reads the
    SQLite ``runs`` table, which only ever gets a row once a checkpoint or the final
    report lands, and ``plan_resume`` needs findings.json.
    """
    monkeypatch.setenv("CODEERRATUM_BASE_URL", "http://stub.invalid/v1")
    monkeypatch.setenv("CODEERRATUM_MODEL", "m")
    monkeypatch.delenv("CODEERRATUM_MODELS", raising=False)
    monkeypatch.setattr(orch, "_make_provider", lambda _spec: object())
    monkeypatch.setattr(orch, "_review_with", _mirroring_review_with(0, then=SystemExit(137)))

    with pytest.raises(SystemExit):
        pipeline.execute_review(repo, "HEAD", mode="quick", max_units=10)

    runs_dir = repo / ".codeerratum" / "runs"
    run_dirs = list(runs_dir.iterdir())
    assert len(run_dirs) == 1, "the scratch directory is whatever the crash leaves behind"
    left = run_dirs[0]
    assert not (left / "calls.json").exists()
    assert not (left / "findings.json").exists()
    assert (left / "units.json").exists()

    assert pipeline.store_for(repo).list_runs() == []
    with pytest.raises(LookupError):
        plan_resume(repo, left.name)
