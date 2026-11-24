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
import threading
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
        # Dual mode: every reviewer records a call for every unit it finished.
        # units_done counts only units covered by ALL reviewers, so the fake
        # must carry both seats' entries.
        return (
            new_collection,
            [
                {"model": "gpt-a", "unit_id": "u3"},
                {"model": "gpt-a", "unit_id": "u4"},
                {"model": "gpt-b", "unit_id": "u3"},
                {"model": "gpt-b", "unit_id": "u4"},
            ],
            [],
        )

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
    dump = json.loads(
        (repo / ".codeerratum" / "runs" / RUN / "findings.json").read_text(encoding="utf-8")
    )
    assert len(dump["findings"]) == 3
    assert dump["comparison"]["resumed"]["batches"] == 2
    # review.md renders the merged set (3 findings, one per row)
    md = (repo / ".codeerratum" / "runs" / RUN / "review.md").read_text(encoding="utf-8")
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


def test_resume_replans_after_waiting_for_the_run_lock(repo, registry, monkeypatch):
    """The lock must serialize planning too, not just the final write.

    A second sitting queued on the same run reads findings.json only after the
    first wrote its merged report. Planning from the snapshot taken before the
    wait would re-review the same unit and restore the stale ledger after
    calls.json already marked it covered.
    """
    _seed_run(repo, [_unit("u1")], [], _old_collection([], []))
    _patch_config(monkeypatch)

    started = threading.Event()
    release = threading.Event()
    reviewed: list[str] = []

    async def fake_run(registry_arg, mode, batch, repo_root, run_id, **kwargs):
        reviewed.extend(u.unit_id for u in batch)
        if len(reviewed) == 1:
            started.set()
            assert release.wait(10), "first sitting was never released"
        return (
            FindingCollection.model_validate(_old_collection([], [])),
            [{"model": "gpt-a", "unit_id": "u1"}, {"model": "gpt-b", "unit_id": "u1"}],
            [],
        )

    monkeypatch.setattr(resume_mod, "run_review", fake_run)
    results: dict[str, object] = {}

    def sitting(label: str) -> None:
        try:
            results[label] = resume_review(repo, RUN, registry=registry, progress=lambda _m: None)
        except BaseException as exc:  # keep the thread alive so both joins can finish
            results[label] = exc

    first = threading.Thread(target=sitting, args=("first",))
    first.start()
    assert started.wait(10), "first sitting never reached the stub"
    second = threading.Thread(target=sitting, args=("second",))
    second.start()
    release.set()
    first.join(20)
    second.join(20)
    assert not first.is_alive() and not second.is_alive(), "a sitting never returned"

    assert reviewed == ["u1"], "the queued sitting must not re-review the same unit"
    assert not isinstance(results["first"], BaseException)
    assert isinstance(results["second"], LookupError)  # nothing left after the first merge


def test_resume_replays_original_pin_from_run_meta(repo, monkeypatch):
    """Tiered quick: the recorded reviewer is the scanner, not the user's pin.

    Re-pinning the scanner would promote it into the verifier seat on the
    second sitting. run_meta.models is the original user pin and must win.
    """
    units = [_unit("u1"), _unit("u2")]
    old = _old_collection([], [])
    old["reviewer_models"] = ["flash"]
    old["falsifier_model"] = "strong"
    old["mode"] = "quick"
    _seed_run(repo, units, [{"model": "flash", "unit_id": "u1"}], old)
    run_dir = repo / ".codeerratum" / "runs" / RUN
    (run_dir / "run_meta.json").write_text(
        json.dumps({"models": ["strong"], "tiered": True, "author_model": None}),
        encoding="utf-8",
    )
    _patch_config(monkeypatch)
    pool = ModelRegistry(
        models=[
            ModelSpec(model_id="flash", base_url="http://a"),
            ModelSpec(model_id="strong", base_url="http://b"),
        ]
    )
    seen: dict = {}

    async def fake_run(registry_arg, mode, batch, repo_root, run_id, **kwargs):
        seen["pinned"] = kwargs.get("pinned")
        seen["tiered"] = kwargs.get("tiered")
        seen["batch"] = [u.unit_id for u in batch]
        return FindingCollection.model_validate(old), [], []

    monkeypatch.setattr(resume_mod, "run_review", fake_run)
    resume_review(repo, RUN, registry=pool, progress=lambda _m: None)

    assert seen["pinned"] == ["strong"], "the original pin, not the scanner, holds the seat"
    assert seen["tiered"] is True
    assert seen["batch"] == ["u2"]


def test_calls_json_is_the_last_ledger_written(repo, registry, monkeypatch):
    """A crash between files must leave the coverage ledger behind the findings."""
    _seed_run(repo, [_unit("u1")], [], _old_collection([], []))
    _patch_config(monkeypatch)
    written: list[str] = []
    real_write = resume_mod.atomic_write_text

    def recording_write(path: Path, text: str) -> None:
        written.append(path.name)
        real_write(path, text)

    monkeypatch.setattr(resume_mod, "atomic_write_text", recording_write)

    async def fake_run(registry_arg, mode, batch, repo_root, run_id, **kwargs):
        return (
            FindingCollection.model_validate(_old_collection([], [])),
            [{"model": "gpt-a", "unit_id": "u1"}, {"model": "gpt-b", "unit_id": "u1"}],
            [],
        )

    monkeypatch.setattr(resume_mod, "run_review", fake_run)
    resume_review(repo, RUN, registry=registry, progress=lambda _m: None)

    assert "findings.json" in written
    assert written[-1] == "calls.json", "calls.json is the coverage commit marker"
