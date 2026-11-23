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

import codeerratum.report.outcomes as outcomes
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
        ("已修复但未通过测试", None),
        ("已修复，测试失败", None),
        ("已修复但测试不通过", None),
        ("修复中", None),
        ("修复进行中", None),
        ("修复未通过", None),
        ("not yet fixed", None),
        ("fixed but tests did not pass", None),
        ("cannot be fixed", None),
        ("non-fixed", None),
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
        ("不是误报", None),
        ("并非误报", None),
        ("not a false positive", None),
        ("not an issue", "rejected"),
        ("not a bug", "rejected"),
        ("no issue", "rejected"),
        ("cannot reproduce", "rejected"),
        ("none fixed", None),
        ("nothing done", None),
        ("none of the tests passed", None),
        ("不是误报，已修复", "accepted"),
        ("已修复，不需要再改", "accepted"),
        # A fix that has not been verified yet is still open. Counting it as
        # accepted would let unverified claims inflate the acceptance rate.
        ("已修复，待验证", None),
        ("已修复，待测试", None),
        ("已修复，待回归", None),
        ("已修复，待确认", None),
        ("fixed, pending verification", None),
        ("fixed but needs verification", None),
        ("已合并", "accepted"),
        ("PR merged", "accepted"),
        ("改完了", "accepted"),
        ("not merged", None),
        ("未合并", None),
        # Variants of "no fix needed" used to fall through to accepted because
        # only "无需修复" was listed and "无须修复" slipped past the negation.
        ("无须修复", "rejected"),
        ("不须修复", "rejected"),
        ("毋须修复", "rejected"),
        ("无需处理", "rejected"),
        ("false alarm", "rejected"),
        ("duplicate", "rejected"),
        ("not a false alarm, fixed", "accepted"),
        # A positive verdict with a trailing qualifier withholds acceptance.
        # These used to be counted as accepted just because they contain the
        # bare words "fixed" / "修复", inflating the acceptance rate.
        ("fixed without full verification", None),
        ("without full verification", None),
        ("部分修复但仍有问题", None),
        ("已修复但仍有问题", None),
        # Compositional English verdicts are matched against the whole cell,
        # so "no test failed" is positive despite containing "failed", while a
        # later unresolved clause still wins.
        ("nothing to fix", "rejected"),
        ("no changes needed", "rejected"),
        ("no longer an issue", "rejected"),
        ("none of these are issues", "rejected"),
        ("no test failed", "accepted"),
        ("all tests passed", "accepted"),
        ("no test failed but not fixed", None),
    ],
)
def test_status_vocabulary(raw, expected):
    assert classify_status(raw) == expected


def test_negated_rejection_survives_removing_the_flat_entry(monkeypatch):
    """The whole-cell rule must own `not a bug`, not just the word list."""
    monkeypatch.setattr(
        outcomes, "_REJECTED", tuple(w for w in outcomes._REJECTED if w != "not a bug")
    )
    assert outcomes.classify_status("not a bug") == "rejected"


def test_flat_rejection_entry_survives_removing_the_whole_cell_rule(monkeypatch):
    """The fixed vocabulary must still work if the compositional rule changes."""
    monkeypatch.setattr(outcomes, "_REJECTED_NEGATIONS", ())
    assert outcomes.classify_status("not a bug") == "rejected"


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


def test_reconcile_fails_when_no_row_can_be_written(tmp_path, monkeypatch):
    """A non-dry reconcile that writes nothing must not look successful."""
    (tmp_path / ".git").mkdir()
    store = ReviewStore(tmp_path / ".codeerratum" / "feedback.db")
    store.save_run(_collection(), "2026-11-23T00:00:00Z")
    report = render_markdown(_collection(), "ts").replace(
        "| F-001 | 待处理 | — |", "| F-999 | 已修复 | x |"
    )
    (tmp_path / "erratumReview.md").write_text(report, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(app, ["reconcile", "--repo", str(tmp_path)])
    assert result.exit_code == 1
    assert "已回填 0" in result.stdout
    assert "没有任何结果写入" in result.stdout


def test_reconcile_dry_run_and_pending_rows_do_not_report_failure(tmp_path, monkeypatch):
    """Previewing a stale report and leaving a valid report pending are both legal."""
    (tmp_path / ".git").mkdir()
    store = ReviewStore(tmp_path / ".codeerratum" / "feedback.db")
    store.save_run(_collection(), "2026-11-23T00:00:00Z")
    report = tmp_path / "erratumReview.md"
    report.write_text(
        render_markdown(_collection(), "ts").replace(
            "| F-001 | 待处理 | — |", "| F-999 | 已修复 | x |"
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    preview = runner.invoke(app, ["reconcile", "--repo", str(tmp_path), "--dry-run"])
    assert preview.exit_code == 0
    assert "已回填 0" in preview.stdout
    assert "预演" in preview.stdout or "[dry run]" in preview.stdout

    report.write_text(render_markdown(_collection(), "ts"), encoding="utf-8")
    pending = runner.invoke(app, ["reconcile", "--repo", str(tmp_path)])
    assert pending.exit_code == 0
    assert "跳过 3" in pending.stdout
    assert "没有任何结果写入" not in pending.stdout


class _FakeStore:
    def __init__(self) -> None:
        ids = ("a" * 16, "b" * 16, "c" * 16)
        self.rows = {fid: {"status": "open", "reason": ""} for fid in ids}

    def statuses_for(self, finding_ids: list[str]) -> dict[str, dict]:
        return {fid: self.rows[fid] for fid in finding_ids if fid in self.rows}

    def set_status(self, finding_id: str, status: str, reason: str = "") -> bool:
        self.rows[finding_id] = {"status": status, "reason": reason}
        return True


def test_list_runs_exposes_machine_drops_separately_from_human_verdicts(tmp_path):
    """`n_dropped` comes from run_rejections (per-reason, authoritative).

    The legacy `n_rejected` column only ever held machine drops — human
    verdicts do not exist when a run is stored — so new callers read
    `n_dropped` and the UI labels it as machine drops, not rejections.
    """
    collection = _collection()
    collection.rejected = [
        {"reason": "refuted", "model": "falsifier", "unit_id": "u1"},
        {"reason": "refuted", "model": "falsifier", "unit_id": "u2"},
        {"reason": "verify_error", "model": "verifier", "unit_id": "u3"},
    ]
    store = ReviewStore(tmp_path / "feedback.db")
    store.save_run(collection, "2026-11-23T00:00:00Z")

    rows = store.list_runs()
    assert len(rows) == 1
    row = rows[0]
    # verify_error is a unit failure: the finding survived, so it is not a drop.
    assert row["n_dropped"] == 2
    assert row["n_failed_units"] == 1
    assert row["n_rejected"] == 3  # legacy column, unchanged meaning
    # after a human rejects one finding, n_dropped must NOT change
    store.set_status("a" * 16, "rejected", "误报")
    rows = store.list_runs()
    assert rows[0]["n_dropped"] == 2, "human verdicts never change machine-drop counts"
    assert rows[0]["n_failed_units"] == 1
    assert rows[0]["n_rejected"] == 3


def test_list_runs_falls_back_for_runs_without_rejection_rows(tmp_path):
    """Runs recorded before run_rejections existed have no reason rows.

    Falling back to the legacy column keeps their history visible; without
    it, every pre-0.6.3 run would suddenly show "0 dropped".
    """
    collection = _collection()
    collection.rejected = [
        {"reason": "anchor_mismatch", "model": "gpt-a"},
        {"reason": "refuted", "model": "falsifier", "unit_id": "u1"},
    ]
    store = ReviewStore(tmp_path / "feedback.db")
    store.save_run(collection, "2026-11-23T00:00:00Z")
    # Simulate an old database: drop the per-reason rows, keep runs.n_rejected.
    with store._conn() as conn:
        conn.execute("DELETE FROM run_rejections")

    row = store.list_runs()[0]
    assert row["n_rejected"] == 2
    assert row["n_dropped"] == 2, "must fall back to the legacy column, not show 0"
    assert row["n_failed_units"] == 0
