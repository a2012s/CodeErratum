"""Resume: continue an interrupted run without re-paying for finished units.

The ledger is the artifacts themselves — units.json (queue), calls.json
(successful review calls only), findings.json (collection dump). These tests
craft that ledger directly and drive `resume_review` with a stubbed
`run_review`, asserting the three promises:

1. finished units are never re-run;
2. a re-run unit replaces its stale provider_error record;
3. the merge is additive and the artifacts/store are rewritten in place.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeerratum import resume as resume_mod
from codeerratum.models import FindingCollection, ModelRegistry, ModelSpec
from codeerratum.models.finding import (
    Anchor,
    DefectCategory,
    Finding,
    Lens,
    Suggestion,
    snippet_hash,
)
from codeerratum.models.unit import Hunk, ReviewUnit
from codeerratum.resume import plan_resume, resume_review

RUN = "abcd1234ef01"


def _unit(uid: str, file: str = "m.py") -> ReviewUnit:
    return ReviewUnit(
        unit_id=uid,
        primary_file=file,
        symbol="f",
        kind="function",
        hunks=[Hunk(old_start=1, old_lines=1, new_start=1, new_lines=2, content="+x = 1\n")],
        changed_lines=2,
        is_public=True,
    )


def _finding(fid: str, unit_id: str, claim: str) -> Finding:
    snippet = "x = 1\n"
    return Finding(
        id=fid,
        unit_id=unit_id,
        lens=Lens.CORRECTNESS,
        category=DefectCategory.BOUNDARY,
        severity="P1",
        claim=claim,
        detail="detail",
        anchors=[
            Anchor(
                file="m.py",
                start_line=1,
                end_line=1,
                snippet=snippet,
                snippet_hash=snippet_hash(snippet),
            )
        ],
        suggestion=Suggestion(explanation="fix", acceptance="pytest -q"),
        source_models=["gpt-a"],
    )


def _seed_run(repo: Path, units: list[ReviewUnit], calls: list[dict], collection: dict) -> None:
    run_dir = repo / ".codeerratum" / "runs" / RUN
    run_dir.mkdir(parents=True)
    (run_dir / "units.json").write_text(
        json.dumps([u.model_dump() for u in units], ensure_ascii=False), encoding="utf-8"
    )
    (run_dir / "calls.json").write_text(json.dumps(calls), encoding="utf-8")
    (run_dir / "findings.json").write_text(
        json.dumps(collection, ensure_ascii=False), encoding="utf-8"
    )
    (run_dir / "static_diags.json").write_text("[]", encoding="utf-8")


def _old_collection(findings: list[Finding], rejected: list[dict]) -> dict:
    return {
        "run_id": RUN,
        "repo": "whatever",
        "commit": "oldcommit12",
        "model_id": "gpt-a,gpt-b",
        "spec_level": "L1",
        "mode": "dual",
        "reviewer_models": ["gpt-a", "gpt-b"],
        "falsifier_model": "deepseek-c",
        "comparison": {
            "mode": "dual",
            "raw_counts": {"gpt-a": 1, "gpt-b": 1},
            "after_clustering": len(findings),
            "after_falsification": len(findings),
            "parse": {"calls": 4, "first_try": 4, "repaired": 0, "failed": 0, "success_rate": 1.0},
        },
        "findings": [f.model_dump(mode="json") for f in findings],
        "refuted_findings": [],
        "rejected": rejected,
        "notes": [],
        "stopped": False,
    }


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "m.py").write_text("x = 1\n", encoding="utf-8")
    return root


@pytest.fixture()
def registry() -> ModelRegistry:
    return ModelRegistry(
        models=[
            ModelSpec(model_id="gpt-a", base_url="http://a"),
            ModelSpec(model_id="gpt-b", base_url="http://b"),
            ModelSpec(model_id="deepseek-c", base_url="http://c"),
        ]
    )


def _patch_config(monkeypatch) -> None:
    # effective_config falls back to env; the resume passes its own registry
    # for the seat pool, so only the config's existence matters here.
    monkeypatch.setenv("CODEERRATUM_BASE_URL", "http://stub.invalid/v1")
    monkeypatch.setenv("CODEERRATUM_MODEL", "gpt-a")


def test_plan_resume_counts_covered_units_per_model_not_per_union(repo, monkeypatch):
    """dual coverage is the per-model intersection, never the union.

    Model A reviewed u1..u3, model B only u1..u2 (it died on u3): u3 is
    partially reviewed and must re-run, not count as covered.
    """
    units = [_unit(f"u{i}") for i in range(1, 5)]
    calls = (
        [{"model": "gpt-a", "unit_id": f"u{i}"} for i in (1, 2, 3)]
        + [{"model": "gpt-b", "unit_id": f"u{i}"} for i in (1, 2)]
        # falsify/verify calls must not leak into the done set
        + [{"model": "deepseek-c", "role": "falsify", "unit_id": "u1"}]
    )
    _seed_run(repo, units, calls, _old_collection([_finding("a" * 16, "u1", "x")], []))

    plan = plan_resume(repo, RUN)
    assert plan["units_done"] == 2  # u1, u2: both models finished them
    assert plan["units_total"] == 4
    assert plan["units_remaining_all"] == 2
    assert sorted(plan["batch_unit_ids"]) == ["u3", "u4"]

    plan_capped = plan_resume(repo, RUN, more=1)
    assert plan_capped["batch_size"] == 1
    assert plan_capped["batch_unit_ids"] == ["u3"]  # risk-ranked order preserved

    plan_path = plan_resume(repo, RUN, paths="nope/")
    assert plan_path["batch_size"] == 0


def test_resume_merges_batches_and_drops_stale_failures(repo, registry, monkeypatch):
    """The interrupted sitting's provider_error for u3 dies when u3 succeeds."""
    units = [_unit(f"u{i}") for i in range(1, 5)]
    calls = [{"model": "gpt-a", "unit_id": f"u{i}"} for i in (1, 2)] + [
        {"model": "gpt-b", "unit_id": f"u{i}"} for i in (1, 2)
    ]
    old_rejected = [
        # stale: u3 failed in sitting #1, resume re-runs it successfully
        {"unit_id": "u3", "model": "gpt-a", "reason": "provider_error", "error": "boom"},
        # fresh: u4's falsifier refuted a claim — not touched by the resume
        {"unit_id": "u4", "model": "deepseek-c", "reason": "refuted", "claim": "假阳性"},
    ]
    old = _old_collection([_finding("a" * 16, "u1", "旧发现")], old_rejected)
    _seed_run(repo, units, calls, old)
    _patch_config(monkeypatch)

    new_collection = FindingCollection(
        run_id=RUN,
        repo=".",
        commit="",
        model_id="gpt-a,gpt-b",
        spec_level="L1",
        mode="dual",
        reviewer_models=["gpt-a", "gpt-b"],
        falsifier_model="deepseek-c",
        findings=[_finding("b" * 16, "u3", "新发现"), _finding("c" * 16, "u4", "另条新发现")],
        rejected=[{"unit_id": "u4", "model": "deepseek-c", "reason": "refuted", "claim": "假阳性"}],
        comparison={
            "mode": "dual",
            "raw_counts": {"gpt-a": 2, "gpt-b": 2},
            "after_clustering": 2,
            "after_falsification": 2,
            "parse": {"calls": 3, "first_try": 2, "repaired": 1, "failed": 0},
        },
    )
    seen: dict = {}

    async def fake_run(registry_arg, mode, batch, repo_root, run_id, **kwargs):
        seen["batch"] = [u.unit_id for u in batch]
        seen["pinned"] = kwargs.get("pinned")
        seen["run_id"] = run_id
        return new_collection, [{"model": "gpt-a", "unit_id": "u3"}], []

    monkeypatch.setattr(resume_mod, "run_review", fake_run)

    outcome = resume_review(repo, RUN, registry=registry, progress=lambda _m: None)

    # same run_id, same artifacts, no re-run of u1/u2
    assert seen["run_id"] == RUN
    assert sorted(seen["batch"]) == ["u3", "u4"]
    assert seen["pinned"] == ["gpt-a", "gpt-b"], "seats must come from the old run"

    merged = outcome.collection
    assert [f.id for f in merged.findings] == ["a" * 16, "b" * 16, "c" * 16]
    assert merged.comparison["resumed"]["units_done"] == 4
    assert merged.comparison["resumed"]["units_total"] == 4
    assert merged.comparison["resumed"]["batches"] == 2
    # raw_counts summed across sittings
    assert merged.comparison["raw_counts"] == {"gpt-a": 3, "gpt-b": 3}
    # stale provider_error gone; the old refuted verdict for u4 is replaced by
    # the fresh one (a re-run unit's machine records are fully replaced)
    reasons = sorted(r["reason"] for r in merged.rejected)
    assert reasons == ["refuted"]
    assert "provider_error" not in reasons
    # parse stats merged with a recomputed rate
    assert merged.comparison["parse"]["calls"] == 7
    assert merged.comparison["parse"]["repaired"] == 1

    # artifacts rewritten in place, findings.json now the merged collection
    dump = json.loads((repo / ".codeerratum" / "runs" / RUN / "findings.json").read_text())
    assert len(dump["findings"]) == 3
    assert dump["comparison"]["resumed"]["batches"] == 2
    # review.md renders the merged set (3 findings, one per row)
    md = (repo / ".codeerratum" / "runs" / RUN / "review.md").read_text()
    assert "旧发现" in md and "新发现" in md


def test_resume_reuses_run_store_rows_without_losing_human_verdicts(repo, registry, monkeypatch):
    """save_run is an upsert: the human 'rejected' status on an old finding stays."""
    from codeerratum.store import ReviewStore

    units = [_unit("u1"), _unit("u2")]
    calls = [{"model": "gpt-a", "unit_id": "u1"}, {"model": "gpt-b", "unit_id": "u1"}]
    _seed_run(repo, units, calls, _old_collection([_finding("a" * 16, "u1", "旧发现")], []))
    _patch_config(monkeypatch)

    store = ReviewStore(repo / ".codeerratum" / "feedback.db")
    old = _old_collection([_finding("a" * 16, "u1", "旧发现")], [])
    store.save_run(FindingCollection.model_validate(old), "ts1")
    store.set_status("a" * 16, "rejected", "误报")

    new_collection = FindingCollection(
        run_id=RUN,
        repo=".",
        commit="",
        model_id="gpt-a,gpt-b",
        spec_level="L1",
        mode="dual",
        reviewer_models=["gpt-a", "gpt-b"],
        findings=[_finding("b" * 16, "u2", "新发现")],
        comparison={
            "mode": "dual",
            "raw_counts": {},
            "after_clustering": 1,
            "after_falsification": 1,
        },
    )

    async def fake_run(registry_arg, mode, batch, repo_root, run_id, **kwargs):
        return new_collection, [], []

    monkeypatch.setattr(resume_mod, "run_review", fake_run)
    resume_review(repo, RUN, registry=registry, progress=lambda _m: None)

    rows = store.list_runs()
    assert len(rows) == 1, "same run_id must update, not append"
    assert rows[0]["n_findings"] == 2
    statuses = store.statuses_for(["a" * 16, "b" * 16])
    assert statuses["a" * 16]["status"] == "rejected", "human verdict survives the upsert"


def test_resume_with_missing_model_errors_before_any_paid_call(repo, monkeypatch):
    """A model dropped from the config is a hard error, not a silent re-seat."""
    units = [_unit("u1"), _unit("u2")]
    calls = [{"model": "gpt-a", "unit_id": "u1"}, {"model": "gpt-b", "unit_id": "u1"}]
    _seed_run(repo, units, calls, _old_collection([], []))
    _patch_config(monkeypatch)
    shrunk = ModelRegistry(models=[ModelSpec(model_id="gpt-a", base_url="http://a")])

    with pytest.raises(Exception, match="gpt-b"):
        resume_review(repo, RUN, registry=shrunk, progress=lambda _m: None)


def test_resume_with_nothing_left_is_a_clean_noop(repo, monkeypatch):
    units = [_unit("u1")]
    calls = [{"model": "gpt-a", "unit_id": "u1"}, {"model": "gpt-b", "unit_id": "u1"}]
    _seed_run(repo, units, calls, _old_collection([], []))
    _patch_config(monkeypatch)
    # the plan still works and reports an empty batch…
    plan = plan_resume(repo, RUN)
    assert plan["batch_size"] == 0
    # …and the review refuses to burn a sitting on it.
    full = ModelRegistry(
        models=[
            ModelSpec(model_id="gpt-a", base_url="http://a"),
            ModelSpec(model_id="gpt-b", base_url="http://b"),
            ModelSpec(model_id="deepseek-c", base_url="http://c"),
        ]
    )
    with pytest.raises(LookupError):
        resume_review(repo, RUN, registry=full, progress=lambda _m: None)
