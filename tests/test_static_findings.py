"""Unclaimed static diagnostics must become executable, gated work orders.

Before this, hard tsc/eslint results were only attached to findings a model had
already reported. A type error on a changed line that no model mentioned stayed
in static_diags.json and the merge gate stayed green. These tests pin the
synthesis step, its deduplication boundaries, and the eval-accounting rule that
tool-produced findings are not charged to any model's precision.
"""

from __future__ import annotations

from pathlib import Path

from codeerratum.eval import SeededBug, score_seeded
from codeerratum.models import ReviewUnit
from codeerratum.models.finding import (
    Anchor,
    DefectCategory,
    Evidence,
    Finding,
    FindingCollection,
    Lens,
    Suggestion,
)
from codeerratum.models.unit import Hunk
from codeerratum.report import gate_sections, is_blocking
from codeerratum.review.static_findings import synthesize_static_findings


def _unit(
    file: str = "src/a.ts",
    new_start: int = 10,
    body: str = "+const x: number = 'bad'\n context\n+const y = 1\n",
) -> ReviewUnit:
    return ReviewUnit(
        unit_id="u1",
        primary_file=file,
        symbol="f",
        kind="function",
        language="typescript",
        hunks=[
            Hunk(
                old_start=new_start,
                old_lines=2,
                new_start=new_start,
                new_lines=3,
                content=body,
            )
        ],
        changed_lines=2,
    )


def _diag(
    line: int = 10,
    *,
    tool: str = "tsc",
    severity: str = "error",
    file: str = "src/a.ts",
    code: str = "TS2322",
    message: str = "Type 'string' is not assignable to type 'number'.",
) -> dict:
    return {
        "tool": tool,
        "file": file,
        "line": line,
        "code": code,
        "message": message,
        "severity": severity,
    }


def _model_finding(line: int = 10) -> Finding:
    return Finding(
        id="a" * 16,
        unit_id="u1",
        lens=Lens.CORRECTNESS,
        category=DefectCategory.BOUNDARY,
        severity="P1",
        claim="核对边界条件",
        detail="触发条件与影响",
        anchors=[Anchor.from_source("src/a.ts", line, line, [f"line {line}\n"])],
        evidence=[Evidence(kind="code_path", summary="推理", strength="soft")],
        suggestion=Suggestion(explanation="改一下", acceptance="pytest -q"),
        source_models=["m1"],
    )


def _collection(*findings: Finding) -> FindingCollection:
    return FindingCollection(
        run_id="r1",
        repo=".",
        commit="abcdef12",
        model_id="m1",
        spec_level="L1",
        mode="quick",
        findings=list(findings),
    )


def test_orphan_tsc_diag_becomes_a_blocking_work_order(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.ts").write_text("line9\nline10\nline11\nline12\n", encoding="utf-8")

    made = synthesize_static_findings([_diag()], [_unit()], [], tmp_path)

    assert len(made) == 1
    f = made[0]
    assert f.origin == "static"
    assert f.source_models == []
    assert f.severity == "P1"
    assert is_blocking(f)
    assert any(e.kind == "static_diag" and e.strength == "hard" for e in f.evidence)
    assert f.anchors[0].start_line == 10
    assert f.suggestion and f.suggestion.acceptance.strip()

    # The gate must see it through the same F-number list as the body.
    blocking, _advisory = gate_sections(_collection(f))
    assert [label for label, _ in blocking] == ["F-001"]


def test_diag_covered_by_a_model_anchor_is_not_duplicated(tmp_path: Path) -> None:
    made = synthesize_static_findings([_diag()], [_unit()], [_model_finding()], tmp_path)
    assert made == []


def test_diag_outside_changed_lines_is_not_synthesized(tmp_path: Path) -> None:
    made = synthesize_static_findings([_diag(line=5)], [_unit()], [], tmp_path)
    assert made == []


def test_warning_stays_advisory(tmp_path: Path) -> None:
    made = synthesize_static_findings([_diag(severity="warning")], [_unit()], [], tmp_path)
    assert len(made) == 1
    assert made[0].severity == "P2"
    assert not is_blocking(made[0])


def test_same_diag_from_several_units_is_synthesized_once(tmp_path: Path) -> None:
    made = synthesize_static_findings([_diag(), _diag()], [_unit(), _unit()], [], tmp_path)
    assert len(made) == 1


def test_static_finding_does_not_charge_model_precision(tmp_path: Path) -> None:
    static = synthesize_static_findings([_diag()], [_unit()], [], tmp_path)[0]
    model = _model_finding()
    collection = _collection(model, static)
    bugs = [SeededBug(file="src/a.ts", start_line=10, end_line=10)]

    scored = score_seeded([collection], bugs)

    assert scored["findings_reported"] == 1
    assert scored["precision"] == 1.0
    assert scored["static_findings"] == 1
