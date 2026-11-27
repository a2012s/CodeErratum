"""Rejected verdicts -> candidate house rules.

Two contracts protect the user's file here: append-only (hand-written rules must
survive byte for byte) and refuse-don't-truncate (a block we write only for the
loader to cut off is a trap). Every test aims at one of those, or at the
aggregation staying honest (threshold, idempotence).
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from codeerratum.cli import app
from codeerratum.models import Anchor, Finding, FindingCollection, Suggestion
from codeerratum.models.finding import DefectCategory, Lens, snippet_hash
from codeerratum.review import MAX_HOUSE_RULES_CHARS
from codeerratum.suggest import append_rules, build_suggestions, plan_append, scope_of


def test_scope_of_keeps_two_segments():
    assert scope_of("src/codeerratum/deep/file.py") == "src/codeerratum"
    assert scope_of("tests/test_x.py") == "tests/test_x.py"
    assert scope_of("solo.py") == "solo.py"
    assert scope_of("") == ""


def test_build_suggestions_threshold_and_aggregation():
    rows = [
        {"category": "boundary", "file": "src/api/a.py", "n": 1},
        {"category": "boundary", "file": "src/api/deep/b.py", "n": 2},
        {"category": "n_plus_one", "file": "tests/t1.py", "n": 1},
        {"category": "boundary", "file": "", "n": 9},  # no scope: not a pattern
        {"category": "", "file": "src/api/c.py", "n": 9},
    ]
    out = build_suggestions(rows, min_count=2)
    assert len(out) == 1  # only src/api boundary crossed the bar
    assert out[0]["scope"] == "src/api"
    assert out[0]["count"] == 3
    assert "src/api/" in out[0]["rule"]


def test_plan_append_keeps_existing_text_verbatim():
    existing = "# 我的规范\n不要过度设计。\n"
    combined, overflow = plan_append(existing, ["- 新规则"])
    assert overflow == 0
    assert combined.startswith(existing.rstrip("\n"))
    assert "- 新规则" in combined


def test_plan_append_skips_duplicates_and_refuses_overflow():
    rule = "- 已存在的规则"
    text, _ = plan_append("# t\n" + rule + "\n", [rule])
    assert text.count(rule) == 1  # re-running suggests nothing new

    big = "x" * (MAX_HOUSE_RULES_CHARS - 5)
    out, overflow = plan_append(big, ["- 再来一条规则进去就超了"])
    assert overflow > 0
    assert out == big  # refusal must not have touched the text at all


def test_append_rules_on_disk_is_idempotent(tmp_path: Path):
    path = tmp_path / ".codeerratum" / "rules.md"
    added, overflow = append_rules(path, ["- 规则一", "- 规则二"])
    assert (added, overflow) == (2, 0)  # rule lines only, not the block header
    again, _ = append_rules(path, ["- 规则一", "- 规则二"])
    assert again == 0
    assert path.read_text(encoding="utf-8").count("- 规则一") == 1


def test_append_rules_overflow_leaves_file_untouched(tmp_path: Path):
    path = tmp_path / "rules.md"
    path.write_text("y" * (MAX_HOUSE_RULES_CHARS - 2), encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    added, overflow = append_rules(path, ["- 超出预算的新规则"])
    assert added == 0 and overflow > 0
    assert path.read_text(encoding="utf-8") == before


def _seed_rejections(store) -> None:
    def finding(fid: str, file: str, category: DefectCategory) -> Finding:
        snippet = "x = 1\n"
        return Finding(
            id=fid,
            unit_id="u",
            lens=Lens.CORRECTNESS,
            category=category,
            severity="P2",
            claim=f"claim {fid}",
            detail="d",
            anchors=[
                Anchor(
                    file=file,
                    start_line=1,
                    end_line=1,
                    snippet=snippet,
                    snippet_hash=snippet_hash(snippet),
                )
            ],
            suggestion=Suggestion(explanation="fix", acceptance="pytest -q"),
        )

    col = FindingCollection(
        run_id="r1",
        repo=".",
        commit="abc12345",
        model_id="m",
        spec_level="L1",
        findings=[
            finding("a" * 16, "src/api/a.py", DefectCategory.BOUNDARY),
            finding("b" * 16, "src/api/b.py", DefectCategory.BOUNDARY),
            finding("c" * 16, "tests/t.py", DefectCategory.N_PLUS_ONE),
        ],
    )
    store.save_run(col, "2026-01-01T00:00:00+00:00")
    for fid in ("a" * 16, "b" * 16, "c" * 16):
        store.set_status(fid, "rejected", "误报")


def test_store_groups_rejections_by_file_and_category(tmp_path: Path):
    from codeerratum.store import ReviewStore

    store = ReviewStore(tmp_path / "feedback.db")
    _seed_rejections(store)
    rows = store.rejected_by_file_category()
    assert {r["file"] for r in rows} == {"src/api/a.py", "src/api/b.py", "tests/t.py"}
    assert sum(r["n"] for r in rows) == 3


def test_cli_suggest_rules_preview_then_append(tmp_path: Path, monkeypatch):
    from codeerratum.pipeline import store_for

    monkeypatch.chdir(tmp_path)
    _seed_rejections(store_for(tmp_path))
    runner = CliRunner()

    preview = runner.invoke(app, ["suggest-rules", "--repo", str(tmp_path)])
    assert preview.exit_code == 0, preview.stdout
    assert "src/api/" in preview.stdout
    assert "boundary" in preview.stdout
    assert not (tmp_path / ".codeerratum" / "rules.md").exists()  # preview writes nothing

    applied = runner.invoke(app, ["suggest-rules", "--repo", str(tmp_path), "--append"])
    assert applied.exit_code == 0, applied.stdout
    rules_file = tmp_path / ".codeerratum" / "rules.md"
    assert rules_file.is_file()
    text = rules_file.read_text(encoding="utf-8")
    assert text.count("boundary") == 1
    assert "n_plus_one" not in text  # one rejection does not make a pattern

    again = runner.invoke(app, ["suggest-rules", "--repo", str(tmp_path), "--append"])
    assert again.exit_code == 0
    assert "未重复追加" in again.stdout
    assert rules_file.read_text(encoding="utf-8") == text


def test_cli_suggest_rules_empty_store(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["suggest-rules", "--repo", str(tmp_path), "--append"])
    assert result.exit_code == 0
    assert "暂无达到阈值" in result.stdout
