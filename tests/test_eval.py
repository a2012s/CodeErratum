"""Offline evaluation harness: seeded recall, falsifier false-kills, label precision.

The property every test protects: a number that reads like quality must be
traceable to an artifact the run actually produced. Silent skips (an unreadable
ledger, an unlabelled refuted row) surface as warnings or ``None`` — never as a
better-looking zero or a dropped denominator.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codeerratum.cli import app
from codeerratum.eval import (
    SeededBug,
    _same_file,
    export_labels,
    format_eval,
    load_seeded_bugs,
    run_collections,
    score_labels,
    score_seeded,
)
from codeerratum.models import Anchor, Finding, FindingCollection, Suggestion
from codeerratum.models.finding import DefectCategory, Lens, snippet_hash
from codeerratum.store import ReviewStore


def _finding(fid: str, file: str, start: int, end: int, models: list[str]) -> Finding:
    snippet = f"line{start}\n"
    return Finding(
        id=fid,
        unit_id=f"u-{fid}",
        lens=Lens.CORRECTNESS,
        category=DefectCategory.BOUNDARY,
        severity="P1",
        claim=f"claim {fid}",
        detail="detail",
        anchors=[
            Anchor(
                file=file,
                start_line=start,
                end_line=end,
                snippet=snippet,
                snippet_hash=snippet_hash(snippet),
            )
        ],
        suggestion=Suggestion(explanation="fix", acceptance="pytest -q"),
        source_models=models,
    )


def _collection(run_id: str = "r1", **kw) -> FindingCollection:
    base = {
        "run_id": run_id,
        "repo": ".",
        "commit": "abc12345",
        "model_id": "m",
        "spec_level": "L1",
    }
    base.update(kw)
    return FindingCollection(**base)


# ---------------------------------------------------------------- manifest


def test_load_seeded_bugs_accepts_both_shapes(tmp_path: Path):
    plain = tmp_path / "a.json"
    plain.write_text(json.dumps([{"file": "x.py", "start_line": 3}]), encoding="utf-8")
    wrapped = tmp_path / "b.json"
    wrapped.write_text(
        json.dumps({"bugs": [{"file": "x.py", "start_line": 3, "end_line": 4}]}),
        encoding="utf-8",
    )
    assert load_seeded_bugs(plain)[0].start_line == 3
    bug = load_seeded_bugs(wrapped)[0]
    assert bug.lines == "3-4"


@pytest.mark.parametrize(
    "content",
    [
        "[]",  # empty ground truth would score every finding as a false positive
        json.dumps([{"start_line": 2}]),  # no file
        json.dumps([{"file": "x.py", "start_line": 0}]),  # line 0 does not exist
        json.dumps([{"file": "x.py", "start_line": 9, "end_line": 3}]),  # inverted range
    ],
)
def test_load_seeded_bugs_rejects_malformed(tmp_path: Path, content: str):
    path = tmp_path / "bugs.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        load_seeded_bugs(path)


def test_same_file_normalises_separators_and_prefixes():
    assert _same_file("src/a/b.py", "b.py")
    assert _same_file("/abs/repo/src/a.py", "src/a.py")
    assert _same_file("src\\a.py", "src/a.py")
    assert not _same_file("src/ax.py", "a.py")  # suffix must respect the path boundary
    assert not _same_file("", "a.py")


# ---------------------------------------------------------------- seeded


def test_score_seeded_recall_and_precision():
    bugs = [
        SeededBug("a.py", 10, 12, category="boundary", note="off by one"),
        SeededBug("b.py", 5, 5, category="race_condition", note="lost update"),
    ]
    col = _collection(
        findings=[
            _finding("f1", "a.py", 11, 11, ["m1"]),  # overlaps bug 1
            _finding("f2", "c.py", 1, 1, ["m1", "m2"]),  # matches nothing: FP
        ]
    )
    out = score_seeded([col], bugs)
    assert out["recall"] == 0.5
    assert out["precision"] == 0.5
    assert out["bugs"][1]["found"] is False
    assert out["missed"][0]["file"] == "b.py"
    row = out["per_model"]["m1"]
    assert (row["assert"], row["hits"], row["refuted"]) == (2, 1, 0)
    assert out["per_model"]["m1"]["precision"] == 0.5
    # m2 asserted one false positive and nothing else.
    assert out["per_model"]["m2"]["precision"] == 0.0


def test_score_seeded_flags_falsifier_kill_of_a_real_bug():
    bugs = [SeededBug("a.py", 10, 12, note="the only sighting was talked out of the report")]
    col = _collection(refuted_findings=[_finding("f1", "a.py", 11, 11, ["m1"])])
    out = score_seeded([col], bugs)
    assert out["recall"] == 0.0
    assert len(out["false_kills"]) == 1
    assert out["missed"] == []  # a kill is not a miss: different round, different owner
    assert out["per_model"]["m1"]["refuted"] == 1


def test_score_seeded_survives_when_one_of_two_sightings_dies():
    bugs = [SeededBug("a.py", 10, 12)]
    col = _collection(
        findings=[_finding("f1", "a.py", 10, 10, ["m1"])],
        refuted_findings=[_finding("f2", "a.py", 12, 12, ["m2"])],
    )
    out = score_seeded([col], bugs)
    assert out["recall"] == 1.0
    assert out["false_kills"] == []


def test_cluster_rollup_rates():
    cols = [
        _collection(
            "r1",
            comparison={
                "raw_counts": {"m1": 4, "m2": 6},
                "after_clustering": 8,
                "after_falsification": 4,
            },
        ),
        _collection(
            "r2",
            comparison={
                "raw_counts": {"m1": 2},
                "after_clustering": 2,
                "after_falsification": 1,
            },
        ),
    ]
    cl = score_seeded(cols, [])["clustering"]
    assert cl["raw"] == 12
    assert cl["after_clustering"] == 10
    assert cl["after_falsification"] == 5
    assert cl["merge_rate"] == round(1 - 10 / 12, 3)
    # kill_rate's denominator is the clustered pool, not the raw one.
    assert cl["kill_rate"] == round(1 - 5 / 10, 3)


def test_cluster_rollup_without_comparison_data_is_honest():
    cl = score_seeded([_collection()], [])["clustering"]
    assert cl == {
        "raw": 0,
        "after_clustering": 0,
        "after_falsification": 0,
        "merge_rate": None,
        "kill_rate": None,
        "refuted_findings": 0,
    }


# ---------------------------------------------------------------- ledgers


def test_run_collections_warns_instead_of_inventing_zero(tmp_path: Path):
    cols, warns = run_collections(tmp_path)
    assert cols == []
    assert any("no run artifacts" in w for w in warns)


def test_run_collections_reads_good_and_flags_broken(tmp_path: Path):
    good = _collection("good", findings=[_finding("f1", "a.py", 1, 1, ["m1"])])
    base = tmp_path / ".codeerratum" / "runs"
    (base / "good").mkdir(parents=True)
    (base / "good" / "findings.json").write_text(good.model_dump_json(), encoding="utf-8")
    (base / "broken").mkdir()
    (base / "broken" / "findings.json").write_text("{not json", encoding="utf-8")
    (base / "gone").mkdir()  # ledger cleaned up after the run was listed

    cols, warns = run_collections(tmp_path, ["good", "broken", "gone"])
    assert [c.run_id for c in cols] == ["good"]
    assert any("unreadable ledger" in w for w in warns)
    assert any("missing" in w for w in warns)


# ---------------------------------------------------------------- labels


def _store_with(tmp_path: Path) -> ReviewStore:
    store = ReviewStore(tmp_path / "feedback.db")
    col = _collection(
        "r1",
        mode="dual",
        findings=[
            _finding("a" * 16, "a.py", 1, 1, ["m1", "m2"]),
            _finding("b" * 16, "b.py", 2, 2, ["m1"]),
        ],
    )
    store.save_run(col, "2026-01-01T00:00:00+00:00")
    store.set_status("a" * 16, "accepted")
    store.set_status("b" * 16, "rejected", "误报")
    return store


def test_export_labels_pre_fills_findings_and_leaves_refuted_open(tmp_path: Path):
    store = _store_with(tmp_path)
    col = _collection(
        "r1",
        refuted_findings=[_finding("c" * 16, "a.py", 3, 3, ["m1"])],
    )
    led = tmp_path / ".codeerratum" / "runs" / "r1"
    led.mkdir(parents=True)
    (led / "findings.json").write_text(col.model_dump_json(), encoding="utf-8")

    rows = export_labels(store, repo_root=tmp_path)
    findings = [r for r in rows if r["kind"] == "finding"]
    refuted = [r for r in rows if r["kind"] == "refuted"]
    assert len(findings) == 2
    by_id = {r["id"]: r for r in findings}
    assert by_id["a" * 16]["human"] == "true"
    assert by_id["b" * 16]["human"] == "false"
    assert by_id["a" * 16]["models"] == ["m1", "m2"]
    # The killed claim has no verdict to pre-fill — that is the whole point of
    # exporting it: a guessed label would fabricate the false-kill rate.
    assert len(refuted) == 1
    assert refuted[0]["human"] == ""
    assert refuted[0]["file"] == "a.py"


def test_score_labels_precision_counts_one_sample_per_model():
    rows = [
        {
            "kind": "finding",
            "id": "f1",
            "category": "boundary",
            "human": "true",
            "models": ["m1", "m2"],
        },
        {"kind": "finding", "id": "f2", "category": "boundary", "human": "false", "models": ["m1"]},
        {
            "kind": "finding",
            "id": "f3",
            "category": "race_condition",
            "human": "",
            "models": ["m2"],
        },
    ]
    out = score_labels(rows)
    assert out["adjudicated"] == 2
    assert out["per_model"]["m1"]["precision"] == 0.5
    assert out["per_model"]["m2"]["precision"] == 1.0
    assert out["per_model"]["m2"]["unlabeled"] == 1
    assert out["per_category"]["race_condition"]["precision"] is None
    assert out["false_kill_rate"] is None  # nothing labelled yet, not "zero kills"


def test_score_labels_false_kill_rate():
    rows = [
        {"kind": "refuted", "id": "x", "human": "true"},  # falsifier killed a real bug
        {"kind": "refuted", "id": "y", "human": "false"},  # right call
        {"kind": "refuted", "id": "z", "human": ""},  # still unlabelled
    ]
    out = score_labels(rows)
    assert out["refuted_total"] == 3
    assert out["refuted_labeled"] == 2
    assert out["false_kill_rate"] == 0.5
    assert out["findings"] == 0


def test_format_eval_sections():
    seeded = score_seeded(
        [_collection(findings=[_finding("f1", "a.py", 1, 1, ["m1"])])],
        [SeededBug("a.py", 1, 1, note="n")],
    )
    labels = score_labels(
        [{"kind": "finding", "human": "true", "models": ["m1"], "category": "boundary"}]
    )
    text = format_eval(seeded, labels)
    assert "Seeded ground truth" in text
    assert "recall      1/1 = 100.0%" in text
    assert "Live labels" in text
    assert format_eval(None, None) == "nothing to score yet"


# ---------------------------------------------------------------- CLI


def test_cli_eval_export_and_score(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from codeerratum.pipeline import store_for

    store = store_for(tmp_path)  # the CLI opens the same .codeerratum/feedback.db
    col = _collection(
        "r1",
        mode="dual",
        findings=[
            _finding("a" * 16, "a.py", 1, 1, ["m1", "m2"]),
            _finding("b" * 16, "b.py", 2, 2, ["m1"]),
        ],
    )
    store.save_run(col, "2026-01-01T00:00:00+00:00")
    store.set_status("a" * 16, "accepted")
    store.set_status("b" * 16, "rejected", "误报")
    runner = CliRunner()

    out_file = tmp_path / "labels.jsonl"
    exported = runner.invoke(app, ["eval", "--repo", str(tmp_path), "--export", str(out_file)])
    assert exported.exit_code == 0, exported.stdout
    rows = [json.loads(line) for line in out_file.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2

    scored = runner.invoke(app, ["eval", "--repo", str(tmp_path), "--json"])
    assert scored.exit_code == 0, scored.stdout
    payload = json.loads(scored.stdout[scored.stdout.index("{") :])
    assert payload["labels"]["adjudicated"] == 2
    assert payload["labels"]["per_model"]["m1"]["precision"] == 0.5


def test_cli_eval_seeded_reads_ledgers(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    col = _collection("run1", findings=[_finding("f1", "a.py", 10, 10, ["m1"])])
    led = tmp_path / ".codeerratum" / "runs" / "run1"
    led.mkdir(parents=True)
    (led / "findings.json").write_text(col.model_dump_json(), encoding="utf-8")
    bugs = tmp_path / "bugs.json"
    bugs.write_text(
        json.dumps({"bugs": [{"file": "a.py", "start_line": 9, "end_line": 11}]}),
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        app, ["eval", "--repo", str(tmp_path), "--seeded", str(bugs), "--json"]
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout[result.stdout.index("{") :])
    assert payload["seeded"]["recall"] == 1.0


def test_export_labels_scoped_to_one_run(tmp_path: Path):
    """--run-id must not pass the findings WHERE binding to the events query.

    adjudication_rows used to reuse the findings query's `(run_id,)` tuple for a
    statement with no placeholder, raising ProgrammingError and breaking every
    scoped eval/export. The events need their own run_id predicate.
    """
    store = _store_with(tmp_path)
    col = _collection(
        "r2",
        mode="dual",
        findings=[_finding("c" * 16, "c.py", 4, 4, ["m3"])],
    )
    store.save_run(col, "2026-01-02T00:00:00+00:00")
    store.set_status("c" * 16, "accepted")

    rows = store.adjudication_rows(run_id="r1")
    assert {r["id"] for r in rows} == {"a" * 16, "b" * 16}
    assert any(r["models"] for r in rows), "assert events must still be joined"

    rows_exported = export_labels(store, run_id="r1")
    assert {row["id"] for row in rows_exported} == {"a" * 16, "b" * 16}


def test_cli_eval_bad_manifest_fails_loudly(tmp_path: Path):
    bugs = tmp_path / "bugs.json"
    bugs.write_text("[]", encoding="utf-8")
    result = CliRunner().invoke(app, ["eval", "--repo", str(tmp_path), "--seeded", str(bugs)])
    assert result.exit_code == 1
