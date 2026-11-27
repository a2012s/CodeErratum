"""Evidence-gated work orders: hard static evidence becomes an enforceable gate.

Evidence.kind/strength and ranking already existed. What was missing is the step
that turns them into disposition: a blocking section in the work order and an exit
code a PR check can consume. These tests pin both, plus the invariant that the
gate index uses the same F-numbers as the body.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from codeerratum import cli
from codeerratum.models.finding import (
    Anchor,
    DefectCategory,
    Evidence,
    Finding,
    FindingCollection,
    Lens,
    Suggestion,
)
from codeerratum.report import gate_sections, is_blocking, labels_from_markdown, render_markdown


def _finding(
    fid: str,
    severity: str = "P1",
    evidence: list[Evidence] | None = None,
    acceptance: str = "pytest -q",
    agreement: str = "unverified",
) -> Finding:
    return Finding(
        id=fid,
        unit_id="u1",
        lens=Lens.CORRECTNESS,
        category=DefectCategory.BOUNDARY,
        severity=severity,
        claim="核对边界条件",
        detail="触发条件与影响",
        anchors=[Anchor.from_source("a.py", 1, 2, ["x = 1\n", "y = 2\n"])],
        evidence=evidence if evidence is not None else [],
        suggestion=Suggestion(explanation="改一下", acceptance=acceptance),
        source_models=["m1"],
        agreement=agreement,
    )


def _collection(*findings: Finding, mode: str = "quick") -> FindingCollection:
    return FindingCollection(
        run_id="r1",
        repo=".",
        commit="abcdef12",
        model_id="m1",
        spec_level="L1",
        mode=mode,
        findings=list(findings),
    )


_HARD_STATIC = [Evidence(kind="static_diag", summary="a.py:1 TS2322", strength="hard")]
_HARD_TEST = [
    Evidence(kind="test_failure", summary="pytest tests/test_a.py:2 失败", strength="hard")
]


def test_blocking_needs_all_three_conditions():
    assert is_blocking(_finding("1" * 16, evidence=_HARD_STATIC))
    # Severity alone is a model hunch, not a gate.
    assert not is_blocking(_finding("2" * 16, severity="P2", evidence=_HARD_STATIC))
    # A hard diagnostic of a different kind is not a mechanical failure.
    assert not is_blocking(
        _finding(
            "3" * 16,
            evidence=[Evidence(kind="code_path", summary="call chain", strength="hard")],
        )
    )
    # Soft evidence cannot be enforced either.
    assert not is_blocking(
        _finding(
            "4" * 16,
            evidence=[Evidence(kind="static_diag", summary="lint hint", strength="soft")],
        )
    )
    # No executable acceptance command means the reader cannot close the item.
    # The model validator refuses to construct one, so mutate a valid finding:
    # old ledgers can still contain such a record.
    empty_acceptance = _finding("5" * 16, evidence=_HARD_STATIC)
    empty_acceptance.suggestion.acceptance = "  "
    assert not is_blocking(empty_acceptance)


def test_gate_index_reuses_the_body_numbering():
    blocking = _finding("a" * 16, evidence=_HARD_STATIC)
    advisory = _finding("b" * 16, severity="P2")
    collection = _collection(advisory, blocking, mode="dual")
    gate_blocking, gate_advisory = gate_sections(collection)

    labels = labels_from_markdown(render_markdown(collection, "ts"))
    assert [label for label, _ in gate_blocking] == [
        label for label, fid in labels.items() if fid == blocking.id
    ]
    assert [label for label, _ in gate_advisory] == [
        label for label, fid in labels.items() if fid == advisory.id
    ]
    assert set(labels.values()) == {blocking.id, advisory.id}


def test_work_order_has_both_disposition_sections():
    collection = _collection(
        _finding("a" * 16, evidence=_HARD_STATIC), _finding("b" * 16, severity="P2")
    )
    text = render_markdown(collection, "ts")
    assert "## 门禁摘要" in text
    assert "### 必须处理（阻断段）" in text
    assert "### 建议处理（建议段）" in text
    assert "F-001 [P1]" in text
    assert "验收：`pytest -q`" in text
    assert "- 处置：**必须处理（阻断段）**" in text
    assert "- 处置：**建议处理（建议段）**" in text


def test_no_blocking_item_is_stated_explicitly():
    text = render_markdown(_collection(_finding("b" * 16, severity="P2")), "ts")
    assert "本次没有阻断项。" in text


class _Outcome:
    def __init__(self, collection: FindingCollection, run_dir: Path):
        self.collection = collection
        self.run_dir = run_dir
        self.out_path = run_dir / "erratumReview.md"
        self.calls: list = []

    def summary(self) -> dict:
        return {
            "findings": len(self.collection.findings),
            "dropped": 0,
            "failed_units": 0,
            "static_diags": 0,
            "calls": 0,
        }


@pytest.mark.parametrize("flag,expected", [([], 0), (["--fail-on-blocking"], cli.GATE_EXIT_CODE)])
def test_review_exit_code_only_fails_when_opted_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, flag: list[str], expected: int
):
    outcome = _Outcome(_collection(_finding("a" * 16, evidence=_HARD_STATIC)), tmp_path)
    monkeypatch.setattr(cli, "execute_review", lambda *_args, **_kwargs: outcome)
    result = CliRunner().invoke(
        cli.app, ["review", "--repo", str(tmp_path), "--out", str(tmp_path / "r.md"), *flag]
    )
    assert result.exit_code == expected, result.output


def test_review_gate_passes_when_only_advisory_findings_exist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    outcome = _Outcome(_collection(_finding("b" * 16, severity="P2")), tmp_path)
    monkeypatch.setattr(cli, "execute_review", lambda *_args, **_kwargs: outcome)
    result = CliRunner().invoke(
        cli.app,
        ["review", "--repo", str(tmp_path), "--out", str(tmp_path / "r.md"), "--fail-on-blocking"],
    )
    assert result.exit_code == 0, result.output
    assert "门禁通过" in result.output


def test_gate_never_truncates_blocking_items():
    findings = [_finding(f"{i:016d}", evidence=_HARD_STATIC) for i in range(12)]
    collection = _collection(*findings)

    blocking, advisory = gate_sections(collection)
    assert len(blocking) == 12
    assert advisory == []
    # Every gate item must have a body entry with its own F-number, otherwise the
    # reader is told to fix something the work order never describes.
    labels = labels_from_markdown(render_markdown(collection, "ts"))
    assert {fid for fid in labels.values()} == {f.id for f in findings}
    assert sorted(labels) == [f"F-{i:03d}" for i in range(1, 13)]


def test_advisory_tail_is_still_capped():
    findings = [_finding(f"{i:016d}", severity="P2") for i in range(12)]
    collection = _collection(*findings)

    blocking, advisory = gate_sections(collection)
    assert blocking == []
    assert len(advisory) == 10
    assert "还有 2 条建议项未在本工单展开" in render_markdown(collection, "ts")


def test_blocking_item_ranked_below_the_cap_still_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    # Ten hard-but-soft-kind findings outrank the one real gate item: severity ties,
    # both carry hard evidence, and agreement decides. Before the fix the render cap
    # dropped the gate item and `--fail-on-blocking` exited 0 on a P1 tsc error.
    higher = [_finding(f"{i:016d}", evidence=_HARD_TEST, agreement="unanimous") for i in range(10)]
    gate_item = _finding("f" * 16, evidence=_HARD_STATIC, agreement="unique")
    collection = _collection(*higher, gate_item)

    blocking, _advisory = gate_sections(collection)
    assert [f.id for _label, f in blocking] == [gate_item.id]
    text = render_markdown(collection, "ts")
    assert "F-011 [P1] 核对边界条件" in text

    outcome = _Outcome(collection, tmp_path)
    monkeypatch.setattr(cli, "execute_review", lambda *_args, **_kwargs: outcome)
    result = CliRunner().invoke(
        cli.app,
        ["review", "--repo", str(tmp_path), "--out", str(tmp_path / "r.md"), "--fail-on-blocking"],
    )
    assert result.exit_code == cli.GATE_EXIT_CODE, result.output
