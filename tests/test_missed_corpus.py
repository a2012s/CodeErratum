"""Adjudication-to-miss pipeline: accepted findings become recall samples.

`eval` already measured misses against the two seeded bugs. This corpus is the
next denominator: a human-accepted finding reported by only one seated model is
a miss for every other seat. It is a sample, not ground truth — the tests pin
that distinction by checking what gets exported and how it is scored.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from codeerratum import pipeline
from codeerratum.cli import app
from codeerratum.eval import export_missed_corpus, score_missed
from codeerratum.models.finding import (
    Anchor,
    DefectCategory,
    Finding,
    FindingCollection,
    Lens,
    Suggestion,
)


def _finding(fid: str, models: list[str], claim: str = "边界没有校验") -> Finding:
    return Finding(
        id=fid,
        unit_id="u1",
        lens=Lens.CORRECTNESS,
        category=DefectCategory.BOUNDARY,
        severity="P1",
        claim=claim,
        detail="触发条件与影响",
        anchors=[Anchor.from_source("src/a.py", 1, 2, ["x = 1\n", "y = 2\n"])],
        suggestion=Suggestion(explanation="补一个判断", acceptance="pytest -q"),
        source_models=models,
    )


def _collection(run_id: str, findings: list[Finding], models: list[str]) -> FindingCollection:
    return FindingCollection(
        run_id=run_id,
        repo=".",
        commit="abc",
        model_id=",".join(models),
        spec_level="L1",
        mode="dual",
        reviewer_models=models,
        findings=findings,
    )


def _write_run(repo: Path, collection: FindingCollection) -> None:
    base = repo / ".codeerratum" / "runs" / collection.run_id
    base.mkdir(parents=True, exist_ok=True)
    (base / "findings.json").write_text(
        json.dumps(collection.model_dump(), ensure_ascii=False), encoding="utf-8"
    )


def test_accepted_finding_becomes_a_miss_only_for_silent_seats(tmp_path: Path):
    old = _collection("old", [_finding("a" * 16, ["m1"]), _finding("b" * 16, ["m1"])], ["m1", "m2"])
    store = pipeline.store_for(tmp_path)
    store.save_run(old, "2026-01-01")
    assert store.set_status("a" * 16, "accepted")
    assert store.set_status("b" * 16, "rejected")

    samples = export_missed_corpus(store, [old])
    assert len(samples) == 1
    sample = samples[0]
    assert sample["model"] == "m2"
    assert sample["finding_id"] == "a" * 16
    assert len(sample["cluster"]) == 12
    assert sample["file"] == "src/a.py"
    assert "api_key" not in json.dumps(sample)


def test_miss_corpus_scores_coverage_of_new_runs(tmp_path: Path):
    old = _collection("old", [_finding("a" * 16, ["m1"])], ["m1", "m2"])
    store = pipeline.store_for(tmp_path)
    store.save_run(old, "2026-01-01")
    store.set_status("a" * 16, "accepted")
    samples = export_missed_corpus(store, [old])

    new = _collection("new", [_finding("c" * 16, ["m2"])], ["m1", "m2"])
    scored = score_missed([new], samples)
    assert scored["samples_total"] == 1
    assert scored["samples_caught"] == 1
    assert scored["sample_recall"] == 1.0
    assert scored["per_model"]["m2"]["caught"] == 1

    # A run that still only reports via m1 leaves the m2 sample uncovered.
    assert score_missed([old], samples)["samples_caught"] == 0


def test_eval_cli_exports_and_scores_miss_corpus(tmp_path: Path):
    old = _collection("old", [_finding("a" * 16, ["m1"])], ["m1", "m2"])
    _write_run(tmp_path, old)
    store = pipeline.store_for(tmp_path)
    store.save_run(old, "2026-01-01")
    store.set_status("a" * 16, "accepted")

    runner = CliRunner()
    corpus = tmp_path / "missed.jsonl"
    exported = runner.invoke(app, ["eval", "--repo", str(tmp_path), "--missed-export", str(corpus)])
    assert exported.exit_code == 0, exported.output
    assert len(corpus.read_text(encoding="utf-8").splitlines()) == 1

    _write_run(tmp_path, _collection("new", [_finding("c" * 16, ["m2"])], ["m1", "m2"]))
    scored = runner.invoke(
        app, ["eval", "--repo", str(tmp_path), "--missed", str(corpus), "--json"]
    )
    assert scored.exit_code == 0, scored.output
    payload = json.loads(scored.output)
    assert payload["missed"]["samples_caught"] == 1
    assert payload["missed"]["sample_recall"] == 1.0


def test_empty_corpus_reports_null_not_zero(tmp_path: Path):
    scored = score_missed([], [])
    assert scored["sample_recall"] is None
    assert scored["samples_total"] == 0
