"""Execution-result recovery: the work order's last table closes the feedback loop.

Every test here protects the same property: what the executor writes must land
in feedback.db as the right verdict, or be reported as skipped/unmatched. A
silent wrong status is worse than no feedback because it moves the acceptance
rate in the wrong direction.
"""

from __future__ import annotations

import re

import pytest
from typer.testing import CliRunner

from codeerratum.cli import app
from codeerratum.models import Anchor, Finding, FindingCollection, Suggestion
from codeerratum.models.finding import DefectCategory, Lens, snippet_hash
from codeerratum.report import classify_status, parse_outcomes, render_markdown
from codeerratum.report.outcomes import apply_outcomes
from codeerratum.store import ReviewStore


def _finding(fid: str, claim: str, line: int = 1) -> Finding:
    snippet = f"x = {line}\n"
    return Finding(
        id=fid,
        unit_id=f"u{line}",
        lens=Lens.CORRECTNESS,
        category=DefectCategory.BOUNDARY,
        severity="P1",
        claim=claim,
        detail="detail",
        anchors=[
            Anchor(
                file="a.py",
                start_line=line,
                end_line=line,
                snippet=snippet,
                snippet_hash=snippet_hash(snippet),
            )
        ],
        suggestion=Suggestion(explanation="fix", acceptance="pytest -q"),
    )


def _collection() -> FindingCollection:
    return FindingCollection(
        run_id="run1",
        repo=".",
        commit="abcdef12",
        model_id="m1",
        spec_level="L1",
        findings=[
            _finding("a" * 16, "空列表取首元素崩溃", 1),
            _finding("b" * 16, "错误码不一致", 5),
            _finding("c" * 16, "重试没有退避", 9),
        ],
    )


def _fill(md: str, fid: str, status: str, verification: str = "—") -> str:
    return md.replace(f"| F-{fid} | 待处理 | — |", f"| F-{fid} | {status} | {verification} |")


def test_round_trip_writes_accepted_rejected_and_keeps_unresolved_open(tmp_path):
    collection = _collection()
    store = ReviewStore(tmp_path / "feedback.db")
    store.save_run(collection, "2026-11-23T00:00:00Z")

    md = render_markdown(collection, "ts")
    md = _fill(md, "001", "已修复", "pytest -q 通过")
    md = _fill(md, "002", "误报", "已有判空")
    md = _fill(md, "003", "未修复", "需要上游改签名")

    parsed = parse_outcomes(md)
    assert parsed.found_table
    assert parsed.problems == []
    counts = apply_outcomes(store, parsed.rows)

    assert counts["rows"] == 3
    assert counts["updated"] == 2
    assert counts["accepted"] == 1
    assert counts["rejected"] == 1
    assert counts["skipped"] == 1
    assert counts["unmatched"] == 0

    statuses = store.statuses_for(["a" * 16, "b" * 16, "c" * 16])
    assert statuses["a" * 16]["status"] == "accepted"
    assert statuses["b" * 16]["status"] == "rejected"
    assert statuses["b" * 16]["reason"] == "误报 | 已有判空"
    assert statuses["c" * 16]["status"] == "open"


def test_legacy_report_without_hidden_marker_still_maps_by_prose_id():
    """Reverting the prose fallback would make every pre-upgrade report unreadable."""
    md = render_markdown(_collection(), "ts")
    legacy = re.sub(r"<!--\s*finding:F-\d{3}=[0-9a-f]+\s*-->", "", md)
    legacy = _fill(legacy, "001", "已修复")

    rows = parse_outcomes(legacy).rows
    assert rows[0].finding_id == "a" * 16


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("已修复", "accepted"),
        ("部分修复（需回归）", "accepted"),
        ("FIXED", "accepted"),
        ("误报", "rejected"),
        ("不适用", "rejected"),
        ("false positive", "rejected"),
        ("未修复", None),
        ("not fixed", None),
        ("待处理", None),
        ("看起来改了", None),
        # Negated accepted words must never be counted as accepted: the accepted
        # vocabulary is substring-matched, so "通过"/"done" live inside these.
        ("未通过", None),
        ("未确认", None),
        ("不通过", None),
        ("未采纳", None),
        ("not done", None),
        ("not passed", None),
        ("not resolved", None),
        ("not accepted", None),
        ("待修复", None),
        ("无法确认", None),
        # "no fix needed" is a false-positive verdict, not an acceptance.
        ("无需修复", "rejected"),
        ("不需要修复", "rejected"),
    ],
)
def test_status_vocabulary(raw, expected):
    assert classify_status(raw) == expected


def test_column_order_and_escaped_pipe_are_tolerated():
    md = render_markdown(_collection(), "ts")
    md = md.replace(
        "| finding_id | 状态 | 验证结果 |\n|---|---|---|",
        "| 状态 | finding_id | 验证结果 |\n|---|---|---|",
    )
    md = md.replace(
        "| F-001 | 待处理 | — |",
        "| 已修复 | F-001 | tests pass \\| 12 cases |",
    )
    parsed = parse_outcomes(md)
    row = next(r for r in parsed.rows if r.finding_id == "a" * 16)
    assert row.status == "accepted"
    assert row.verification == "tests pass | 12 cases"


def test_unknown_reference_and_missing_table_are_reported_not_guessed():
    md = render_markdown(_collection(), "ts").replace(
        "| F-001 | 待处理 | — |", "| F-999 | 已修复 | x |"
    )
    parsed = parse_outcomes(md)
    assert any("F-999" in p for p in parsed.problems)
    assert parsed.rows[0].finding_id is None

    assert parse_outcomes("# no table here").found_table is False


def test_last_row_for_a_finding_wins():
    md = render_markdown(_collection(), "ts")
    md = md.replace("| F-001 | 待处理 | — |", "| F-001 | 误报 | first try |")
    md += "| F-001 | 已修复 | fixed later |\n"
    parsed = parse_outcomes(md)
    counts = apply_outcomes(_FakeStore(), parsed.rows)
    assert counts["updated"] == 1
    assert counts["skipped"] == 2
    assert counts["accepted"] == 1
    assert counts["rejected"] == 0


def test_cli_reconcile_defaults_to_erratum_review_and_honours_dry_run(tmp_path, monkeypatch):
    collection = _collection()
    # Make tmp_path its own repository root. Without a .git here, find_repo_root
    # walks up into the project checkout when --basetemp lives inside it, so the
    # CLI writes to the project's feedback.db and this test silently checks the
    # wrong database.
    (tmp_path / ".git").mkdir()
    store = ReviewStore(tmp_path / ".codeerratum" / "feedback.db")
    store.save_run(collection, "2026-11-23T00:00:00Z")
    report = tmp_path / "erratumReview.md"
    report.write_text(_fill(render_markdown(collection, "ts"), "001", "已修复"), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    preview = runner.invoke(app, ["reconcile", "--repo", str(tmp_path), "--dry-run"])
    assert preview.exit_code == 0
    assert "预演" in preview.stdout or "[dry run]" in preview.stdout
    assert "已回填 1" in preview.stdout
    assert store.statuses_for(["a" * 16])["a" * 16]["status"] == "open"

    applied = runner.invoke(app, ["reconcile", "--repo", str(tmp_path)])
    assert applied.exit_code == 0
    # Assert the real write, not just a successful exit: zero rows written used
    # to pass this test as long as the CLI returned 0.
    assert "已回填 1" in applied.stdout
    assert store.statuses_for(["a" * 16])["a" * 16]["status"] == "accepted"


def test_reconcile_finds_work_order_at_repo_root_from_a_subdirectory(tmp_path, monkeypatch):
    """README promises identical behaviour from any subdirectory of the repo."""
    (tmp_path / ".git").mkdir()
    collection = _collection()
    store = ReviewStore(tmp_path / ".codeerratum" / "feedback.db")
    store.save_run(collection, "2026-11-23T00:00:00Z")
    (tmp_path / "erratumReview.md").write_text(
        _fill(render_markdown(collection, "ts"), "001", "已修复"), encoding="utf-8"
    )
    sub = tmp_path / "src" / "deep"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)

    result = CliRunner().invoke(app, ["reconcile"])
    assert result.exit_code == 0
    assert store.statuses_for(["a" * 16])["a" * 16]["status"] == "accepted"


def test_apply_outcomes_counts_failed_writes_as_unmatched():
    """A row deleted mid-reconcile must not be reported as successfully written."""
    store = _FakeStore()
    parsed = parse_outcomes(_fill(render_markdown(_collection(), "ts"), "001", "已修复"))
    store.set_status = lambda *_a, **_kw: False  # type: ignore[method-assign]
    counts = apply_outcomes(store, parsed.rows)
    assert counts["updated"] == 0
    assert counts["accepted"] == 0
    assert counts["unmatched"] >= 1


class _FakeStore:
    def __init__(self) -> None:
        ids = ("a" * 16, "b" * 16, "c" * 16)
        self.rows = {fid: {"status": "open", "reason": ""} for fid in ids}

    def statuses_for(self, finding_ids: list[str]) -> dict[str, dict]:
        return {fid: self.rows[fid] for fid in finding_ids if fid in self.rows}

    def set_status(self, finding_id: str, status: str, reason: str = "") -> bool:
        self.rows[finding_id] = {"status": status, "reason": reason}
        return True
