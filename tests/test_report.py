from codeerratum.models import Anchor, Finding, Suggestion
from codeerratum.models.finding import DefectCategory, FindingCollection, Lens, snippet_hash
from codeerratum.report import render_markdown


def test_render_work_order(tmp_path):
    (tmp_path / "a.py").write_text("first = items[0]\n", encoding="utf-8")
    snippet = "first = items[0]\n"
    f = Finding(
        id="abc123",
        unit_id="u1",
        lens=Lens.CORRECTNESS,
        category=DefectCategory.BOUNDARY,
        severity="P0",
        claim="空列表取首元素崩溃",
        detail="当 items 为空时抛 IndexError，接口返回 500。",
        anchors=[
            Anchor(
                file="a.py",
                start_line=1,
                end_line=1,
                snippet=snippet,
                snippet_hash=snippet_hash(snippet),
            )
        ],
        suggestion=Suggestion(explanation="先判空", acceptance="pytest -q 通过"),
    )
    col = FindingCollection(
        run_id="r1",
        repo=str(tmp_path),
        commit="abcdef123456",
        model_id="m1",
        spec_level="L1",
        findings=[f],
        rejected=[{"reason": "anchor_mismatch", "claim": "假问题", "cited": "a.py:9"}],
    )
    out = render_markdown(col, "2026-01-01T00:00:00Z")
    assert "# Code Review 工单" in out
    assert "F-001 [P0]" in out
    assert "执行协议" in out
    assert "空列表取首元素崩溃" in out
    assert "pytest -q 通过" in out
    assert "已丢弃的发现" in out
    assert "执行结果（由执行方 AI 填写）" in out


def test_empty_findings(tmp_path):
    col = FindingCollection(run_id="r", repo=".", commit="abc", model_id="m", spec_level="L1")
    out = render_markdown(col, "ts")
    assert "本次变更未发现明确问题" in out
