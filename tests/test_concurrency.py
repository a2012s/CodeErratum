"""Unit calls run a few at a time, not one after another.

Measured on a real project: one unit is 82–150 s of generation, and a 44-unit
serial sweep was an hour of waiting for exactly the same tokens. The wall clock
was the product of per-call latency and the unit count — the calls are pure I/O
wait, so widening the gate is the whole fix.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from codeerratum.models import ModelSpec
from codeerratum.providers import Completion
from codeerratum.review import orchestrator


def _units(count: int) -> list[SimpleNamespace]:
    return [SimpleNamespace(unit_id=f"u{i}", primary_file=f"mod_{i}.py") for i in range(count)]


def _spec() -> ModelSpec:
    return ModelSpec(model_id="m", base_url="http://stub.invalid/v1", api_key="k")


def _patch_review(monkeypatch, delay: float, seen: dict) -> None:
    """Replace the per-unit call with a sleep, and record overlap."""

    async def fake_review_unit(provider, model_id, unit, repo_root, run_id, house_rules, sources, **_kwargs):
        seen["started"] += 1
        seen["peak"] = max(seen["peak"], seen["started"])
        await asyncio.sleep(delay)
        seen["started"] -= 1
        return (
            [],
            [],
            Completion(text="{}", raw_request_hash="h", latency_ms=int(delay * 1000)),
            [],
        )

    monkeypatch.setattr(orchestrator, "review_unit", fake_review_unit)


def _run(units, seen):
    return asyncio.run(
        orchestrator._review_with(
            provider=SimpleNamespace(),
            spec=_spec(),
            units=units,
            repo_root=None,
            run_id="r",
            progress=lambda _m: None,
        )
    )


def test_units_run_in_parallel_by_default(monkeypatch):
    seen = {"started": 0, "peak": 0}
    _patch_review(monkeypatch, 0.2, seen)
    monkeypatch.delenv(orchestrator.UNIT_CONCURRENCY_ENV, raising=False)

    t0 = time.monotonic()
    per_unit, rejected, calls, _diags = _run(_units(8), seen)
    elapsed = time.monotonic() - t0

    assert len(calls) == 8, "every unit must still be reviewed"
    assert seen["peak"] > 1, "calls must overlap, not queue"
    # 8 × 0.2 s serially is 1.6 s; four at a time is about 0.4 s.
    assert elapsed < 1.2, f"serial behaviour came back ({elapsed:.2f}s)"
    assert rejected == []


def test_concurrency_one_restores_the_serial_order(monkeypatch):
    """The env override is the escape hatch for providers with tight rate limits."""
    seen = {"started": 0, "peak": 0}
    _patch_review(monkeypatch, 0.1, seen)
    monkeypatch.setenv(orchestrator.UNIT_CONCURRENCY_ENV, "1")

    t0 = time.monotonic()
    _run(_units(4), seen)
    elapsed = time.monotonic() - t0

    assert seen["peak"] == 1
    assert elapsed >= 0.4


def test_bad_env_value_falls_back_instead_of_crashing(monkeypatch):
    monkeypatch.setenv(orchestrator.UNIT_CONCURRENCY_ENV, "not-a-number")
    assert orchestrator.unit_concurrency() == orchestrator.DEFAULT_UNIT_CONCURRENCY
    monkeypatch.setenv(orchestrator.UNIT_CONCURRENCY_ENV, "0")
    assert orchestrator.unit_concurrency() == orchestrator.DEFAULT_UNIT_CONCURRENCY


def test_a_failing_unit_does_not_kill_its_neighbours(monkeypatch):
    async def boom(provider, model_id, unit, repo_root, run_id, house_rules, sources, **_kwargs):
        if unit.unit_id == "u1":
            raise RuntimeError("provider exploded")
        return (
            [],
            [],
            Completion(text="{}", raw_request_hash="h", latency_ms=1),
            [],
        )

    monkeypatch.setattr(orchestrator, "review_unit", boom)
    per_unit, rejected, calls, _ = _run(_units(3), {"started": 0, "peak": 0})

    assert len(calls) == 2
    assert [r["unit_id"] for r in rejected] == ["u1"]
    assert "provider exploded" in rejected[0]["error"]


@pytest.mark.parametrize("count", [1, 2, 5])
def test_small_batches_still_complete(monkeypatch, count):
    seen = {"started": 0, "peak": 0}
    _patch_review(monkeypatch, 0.05, seen)
    _, _, calls, _ = _run(_units(count), seen)
    assert len(calls) == count
