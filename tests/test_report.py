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


def test_unit_failures_render_with_context_not_empty_bullets(tmp_path):
    """provider_error / verify_error entries have no claim — they used to render
    as empty bullets under a drop title that did not apply to them."""
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    col = FindingCollection(
        run_id="r1",
        repo=str(tmp_path),
        commit="abcdef123456",
        model_id="m1",
        spec_level="L1",
        rejected=[
            {
                "reason": "verify_error",
                "model": "verifier",
                "unit_id": "src/api.py::handle",
                "error": "connection reset by peer",
                "error_hint": "端点 5xx",
            },
            {
                "reason": "provider_error",
                "model": "gpt-a",
                "unit_id": "src/b.py::main",
                "error": "timeout",
                "error_hint": "读取超时",
            },
        ],
    )
    out = render_markdown(col, "2026-01-01T00:00:00Z")

    assert "## 失败的单元" in out
    assert "src/api.py::handle" in out, "unit_id must identify the failed unit"
    assert "端点 5xx" in out, "error_hint must carry the reason"
    assert "src/b.py::main" in out
    assert "- `verify_error` [verifier] src/api.py::handle — 端点 5xx" in out
    assert "- `provider_error` [gpt-a] src/b.py::main — 读取超时" in out
    # no empty bullets, and nothing leaked into the drop section
    assert "- `verify_error` [verifier]  —" not in out
    assert "- `provider_error` [gpt-a]  —" not in out
    assert "## 已丢弃的发现" not in out


def test_drops_and_unit_failures_render_in_separate_sections(tmp_path):
    col = FindingCollection(
        run_id="r1",
        repo=".",
        commit="abc",
        model_id="m",
        spec_level="L1",
        rejected=[
            {"reason": "refuted", "model": "falsifier", "claim": "假阳性", "cited": "a.py:3"},
            {"reason": "verify_error", "model": "verifier", "unit_id": "u9", "error_hint": "boom"},
        ],
    )
    out = render_markdown(col, "ts")
    drops_idx = out.index("## 已丢弃的发现")
    fails_idx = out.index("## 失败的单元")
    assert drops_idx < fails_idx
    assert "`refuted` [falsifier] 假阳性 — a.py:3" in out
    # the drop section must not contain the unit failure line
    assert out[drops_idx:fails_idx].count("verify_error") == 0


def test_drop_list_truncation_says_so(tmp_path):
    col = FindingCollection(
        run_id="r1",
        repo=".",
        commit="abc",
        model_id="m",
        spec_level="L1",
        rejected=[
            {"reason": "refuted", "model": "f", "claim": f"claim {i}", "cited": "a.py:1"}
            for i in range(13)
        ],
    )
    out = render_markdown(col, "ts")
    assert "共 13 条，仅显示前 10 条" in out
    assert "claim 12" not in out  # the 13th stays out as before
