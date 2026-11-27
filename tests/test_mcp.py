"""MCP server tests — protocol shape plus one end-to-end tool call.

The protocol surface is small on purpose: initialize, tools/list, tools/call. What
matters is that a malformed session cannot kill the process and that stdout stays
pure JSON, because the client parses it line by line.
"""

from __future__ import annotations

import io
import json
import subprocess

import pytest

import codeerratum.review.orchestrator as orchestrator
from codeerratum.mcp import PROTOCOL_VERSION, TOOLS, handle, serve
from codeerratum.models import Anchor, Finding, FindingCollection, Suggestion
from codeerratum.pipeline import store_for
from codeerratum.providers.base import Completion, Usage
from codeerratum.review.schema import LLMAnchor, LLMFinding, LLMReview, LLMSuggestion


def call(method: str, params: dict | None = None, request_id: int = 1):
    return handle({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})


# ---- protocol -----------------------------------------------------------


def test_initialize_advertises_the_tools_capability():
    result = call("initialize")["result"]
    assert result["protocolVersion"] == PROTOCOL_VERSION
    assert "tools" in result["capabilities"]
    assert result["serverInfo"]["name"] == "codeerratum"
    assert result["serverInfo"]["version"]


def test_notifications_get_no_response():
    """A notification has no id; answering it makes strict clients unhappy."""
    assert handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_tools_list_exposes_every_tool_with_a_schema():
    tools = call("tools/list")["result"]["tools"]
    assert [t["name"] for t in tools] == [
        "review_diff",
        "list_runs",
        "get_finding",
        "accept_finding",
        "reject_finding",
        "reconcile_report",
        "get_stats",
        "check_docs_drift",
    ]
    for tool in TOOLS:
        assert tool["inputSchema"]["type"] == "object"
        assert tool["description"]


def test_unknown_method_is_rejected_not_ignored():
    assert call("does/not/exist")["error"]["code"] == -32601


def test_unknown_tool_is_rejected():
    assert call("tools/call", {"name": "nope"})["error"]["code"] == -32602


def test_ping_answers():
    assert call("ping")["result"] == {}


def test_serve_survives_bad_json_and_keeps_going():
    lines = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
        "this is not json",
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}),
    ]
    out = io.StringIO()
    assert serve(stdin=io.StringIO("\n".join(lines) + "\n"), stdout=out) == 0
    responses = [json.loads(line) for line in out.getvalue().splitlines()]
    assert responses[0]["id"] == 1
    assert responses[1]["error"]["code"] == -32700
    assert responses[2]["result"] == {}


def test_blank_lines_are_ignored():
    out = io.StringIO()
    serve(stdin=io.StringIO("\n\n   \n"), stdout=out)
    assert out.getvalue() == ""


# ---- tools --------------------------------------------------------------


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.io"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "m.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    (tmp_path / "m.py").write_text("def foo():\n    x = []\n    return x[0]\n", encoding="utf-8")
    return tmp_path


def test_list_runs_on_a_fresh_repo_says_so(repo):
    result = call("tools/call", {"name": "list_runs", "arguments": {"repo": str(repo)}})
    assert result["result"]["isError"] is False
    assert "还没有运行记录" in result["result"]["content"][0]["text"]


def test_get_finding_without_an_id_is_an_error(repo):
    result = call("tools/call", {"name": "get_finding", "arguments": {"repo": str(repo)}})
    assert result["result"]["isError"] is True


def test_get_finding_unknown_prefix_is_an_error(repo):
    result = call(
        "tools/call",
        {"name": "get_finding", "arguments": {"repo": str(repo), "finding_id": "deadbeef"}},
    )
    assert result["result"]["isError"] is True


def test_check_docs_drift_without_a_baseline(repo):
    result = call("tools/call", {"name": "check_docs_drift", "arguments": {"repo": str(repo)}})
    assert "尚无签名基线" in result["result"]["content"][0]["text"]


class StubProvider:
    family = "openai"

    def __init__(self, line: int, snippet: str):
        self.line = line
        self.snippet = snippet

    async def complete(self, model_id, messages, schema, temperature=0.0, max_tokens=2048):
        review = LLMReview(
            summary="stub",
            findings=[
                LLMFinding(
                    lens="correctness",
                    category="boundary",
                    severity="P1",
                    claim="空列表取首元素会崩",
                    detail="items 为空时抛 IndexError，接口 500。",
                    anchor=LLMAnchor(
                        file="m.py", start_line=self.line, end_line=self.line, snippet=self.snippet
                    ),
                    suggestion=LLMSuggestion(
                        explanation="取首元素前判空", acceptance="python -c 'import m; m.foo()'"
                    ),
                )
            ],
        )
        return Completion(
            text=json.dumps(review.model_dump(mode="json")),
            parsed=review,
            usage=Usage(input_tokens=10, output_tokens=5),
            raw_request_hash="stub",
            latency_ms=1,
        )


@pytest.fixture
def model_env(monkeypatch, repo):
    monkeypatch.setenv("CODEERRATUM_BASE_URL", "http://stub.invalid/v1")
    monkeypatch.setenv("CODEERRATUM_MODEL", "gpt-stub")
    monkeypatch.setenv("CODEERRATUM_MODE", "quick")
    monkeypatch.delenv("CODEERRATUM_MODELS", raising=False)
    source_lines = (repo / "m.py").read_text().splitlines(keepends=True)
    line = next(i for i, text in enumerate(source_lines, 1) if "return x[0]" in text)
    stub = StubProvider(line, source_lines[line - 1])
    monkeypatch.setattr(orchestrator, "_make_provider", lambda spec: stub)
    return repo


def test_omitted_max_units_uses_the_same_auto_budget_as_cli(repo, monkeypatch):
    """MCP default must not silently review a sixth of the diff."""
    import codeerratum.mcp.server as mcp_server
    from codeerratum.pipeline import ConfigError

    seen: dict = {}

    def fake_execute(root, **kwargs):
        seen.update(kwargs)
        raise ConfigError("captured")

    monkeypatch.setattr(mcp_server, "execute_review", fake_execute)
    call("tools/call", {"name": "review_diff", "arguments": {"repo": str(repo)}})
    assert seen["max_units"] is None
    assert seen["author_model"] is None
    assert seen["paths"] == ""
    assert seen["tiered"] is True
    assert seen["include_non_code"] is False


def test_review_diff_forwards_the_exposed_cli_parameters(repo, monkeypatch):
    import codeerratum.mcp.server as mcp_server
    from codeerratum.pipeline import ConfigError

    seen: dict = {}

    def fake_execute(root, **kwargs):
        seen.update(kwargs)
        raise ConfigError("captured")

    monkeypatch.setattr(mcp_server, "execute_review", fake_execute)
    call(
        "tools/call",
        {
            "name": "review_diff",
            "arguments": {
                "repo": str(repo),
                "author_model": "gpt-author",
                "paths": "src/api",
                "tiered": False,
                "include_non_code": True,
            },
        },
    )
    assert seen["author_model"] == "gpt-author"
    assert seen["paths"] == "src/api"
    assert seen["tiered"] is False
    assert seen["include_non_code"] is True


def test_token_budget_matches_the_cli_unset_zero_split(repo, monkeypatch):
    """Omitted, 0 and a number are three different answers on every entry point.

    The pipeline reads unset as "consult $CODEERRATUM_TOKEN_BUDGET" and 0 as
    "explicitly uncapped". An MCP client that collapses them would let a tool
    call override the budget the user exported in their shell.
    """
    import codeerratum.mcp.server as mcp_server
    from codeerratum.pipeline import ConfigError

    seen: dict = {}

    def fake_execute(root, **kwargs):
        seen.update(kwargs)
        raise ConfigError("captured")

    monkeypatch.setattr(mcp_server, "execute_review", fake_execute)
    call("tools/call", {"name": "review_diff", "arguments": {"repo": str(repo)}})
    assert seen["token_budget"] is None

    call(
        "tools/call",
        {"name": "review_diff", "arguments": {"repo": str(repo), "token_budget": 50000}},
    )
    assert seen["token_budget"] == 50000

    call(
        "tools/call",
        {"name": "review_diff", "arguments": {"repo": str(repo), "token_budget": 0}},
    )
    assert seen["token_budget"] == 0


def test_review_diff_advertises_token_budget_in_its_schema():
    """The control has to be visible where clients discover capabilities."""
    tool = next(t for t in call("tools/list")["result"]["tools"] if t["name"] == "review_diff")
    assert "token_budget" in tool["inputSchema"]["properties"]


def _finding_for(repo, claim="空列表取首元素崩溃", finding_id=""):
    source_lines = (repo / "m.py").read_text(encoding="utf-8").splitlines(keepends=True)
    line = next(i for i, text in enumerate(source_lines, 1) if "return x[0]" in text)
    return Finding(
        id=finding_id or Finding.content_id("u1", claim, [f"m.py:{line}"]),
        unit_id="u1",
        lens="correctness",
        category="boundary",
        severity="P1",
        claim=claim,
        detail="当 x 为空时抛 IndexError。",
        anchors=[Anchor.from_source("m.py", line, line, source_lines)],
        suggestion=Suggestion(explanation="判空", acceptance="pytest -q"),
    )


def _seed_finding(repo):
    """Persist one real anchored finding so the feedback tools have something to act on."""
    finding = _finding_for(repo)
    collection = FindingCollection(
        run_id=f"run-{finding.id}",
        repo=str(repo),
        commit="c0",
        model_id="stub",
        spec_level="L1",
        findings=[finding],
    )
    store = store_for(repo)
    store.save_run(collection, "2026-11-23T00:00:00+00:00")
    return store, finding.id


def test_accept_finding_is_idempotent(repo):
    store, finding_id = _seed_finding(repo)
    args = {"repo": str(repo), "finding_id": finding_id}

    first = call("tools/call", {"name": "accept_finding", "arguments": args})["result"]
    second = call("tools/call", {"name": "accept_finding", "arguments": args})["result"]

    assert first["isError"] is False
    assert second["isError"] is False
    assert store.statuses_for([finding_id])[finding_id]["status"] == "accepted"
    assert json.loads(second["content"][0]["text"])["previous_status"] == "accepted"


def test_get_finding_rejects_an_ambiguous_prefix(repo):
    first = _finding_for(repo, claim="空列表取首元素崩溃", finding_id="abcd0001")
    second = _finding_for(repo, claim="空列表取末元素崩溃", finding_id="abcd0002")
    collection = FindingCollection(
        run_id="run-ambiguous",
        repo=str(repo),
        commit="c0",
        model_id="stub",
        spec_level="L1",
        findings=[first, second],
    )
    store_for(repo).save_run(collection, "2026-11-23T00:00:00+00:00")

    result = call(
        "tools/call",
        {
            "name": "get_finding",
            "arguments": {"repo": str(repo), "finding_id": "abcd"},
        },
    )["result"]

    assert result["isError"] is True
    assert "匹配到多条" in result["content"][0]["text"]


def test_reject_finding_requires_a_reason_and_is_idempotent(repo):
    store, finding_id = _seed_finding(repo)

    missing = call(
        "tools/call",
        {
            "name": "reject_finding",
            "arguments": {"repo": str(repo), "finding_id": finding_id},
        },
    )["result"]
    assert missing["isError"] is True

    args = {"repo": str(repo), "finding_id": finding_id, "reason": "误报：上游已经判空"}
    first = call("tools/call", {"name": "reject_finding", "arguments": args})["result"]
    second = call("tools/call", {"name": "reject_finding", "arguments": args})["result"]

    assert first["isError"] is False
    assert second["isError"] is False
    status = store.statuses_for([finding_id])[finding_id]
    assert status["status"] == "rejected"
    assert status["reason"] == "误报：上游已经判空"


def test_reconcile_report_dry_run_then_applies(repo):
    store, finding_id = _seed_finding(repo)
    report = repo / "erratumReview.md"
    report.write_text(
        "# Review\n\n## 执行结果\n\n"
        "| 编号 | 状态 | 验证结果 |\n"
        "|---|---|---|\n"
        f"| {finding_id} | 误报 | 已确认 |\n",
        encoding="utf-8",
    )
    args = {"repo": str(repo), "report": "erratumReview.md"}

    dry = call(
        "tools/call",
        {"name": "reconcile_report", "arguments": args | {"dry_run": True}},
    )["result"]
    dry_payload = json.loads(dry["content"][0]["text"])
    assert dry["isError"] is False
    assert dry_payload["dry_run"] is True
    assert dry_payload["counts"]["rejected"] == 1
    assert store.statuses_for([finding_id])[finding_id]["status"] == "open"

    applied = call("tools/call", {"name": "reconcile_report", "arguments": args})["result"]
    payload = json.loads(applied["content"][0]["text"])
    assert applied["isError"] is False
    assert payload["counts"]["updated"] == 1
    assert payload["counts"]["rejected"] == 1
    assert store.statuses_for([finding_id])[finding_id]["status"] == "rejected"


def test_reconcile_report_without_an_execution_table_is_an_error(repo):
    (repo / "erratumReview.md").write_text("# Review\n\nno table here\n", encoding="utf-8")
    result = call(
        "tools/call",
        {
            "name": "reconcile_report",
            "arguments": {"repo": str(repo), "report": "erratumReview.md"},
        },
    )["result"]
    assert result["isError"] is True
    assert "执行结果" in result["content"][0]["text"]


def test_reconcile_report_with_only_unknown_ids_is_an_error(repo):
    (repo / "erratumReview.md").write_text(
        "## 执行结果\n\n| 编号 | 状态 | 验证结果 |\n|---|---|---|\n"
        "| deadbeefdeadbeef | 误报 | 已确认 |\n",
        encoding="utf-8",
    )
    result = call(
        "tools/call",
        {
            "name": "reconcile_report",
            "arguments": {"repo": str(repo), "report": "erratumReview.md"},
        },
    )["result"]
    assert result["isError"] is True
    assert "没有任何编号匹配" in result["content"][0]["text"]


def test_get_stats_returns_the_feedback_summary(repo):
    _seed_finding(repo)
    result = call("tools/call", {"name": "get_stats", "arguments": {"repo": str(repo)}})
    payload = json.loads(result["result"]["content"][0]["text"])
    assert result["result"]["isError"] is False
    assert payload["findings_total"] == 1
    assert "acceptance_rate" in payload
    assert "dropped_by_reason" in payload


def test_review_diff_returns_the_work_order(model_env):
    result = call(
        "tools/call",
        {"name": "review_diff", "arguments": {"repo": str(model_env), "diff": "HEAD"}},
    )
    payload = result["result"]
    assert payload["isError"] is False
    text = payload["content"][0]["text"]
    assert "Code Review 工单" in text
    assert "空列表取首元素会崩" in text
    assert "运行产物" in text


def test_review_diff_summary_format_is_json(model_env):
    result = call(
        "tools/call",
        {
            "name": "review_diff",
            "arguments": {"repo": str(model_env), "diff": "HEAD", "format": "summary"},
        },
    )
    data = json.loads(result["result"]["content"][0]["text"])
    assert data["findings"] == 1
    assert data["mode"] == "quick"


def test_review_diff_without_changes_returns_a_message(repo, monkeypatch):
    monkeypatch.setenv("CODEERRATUM_BASE_URL", "http://stub.invalid/v1")
    monkeypatch.setenv("CODEERRATUM_MODEL", "gpt-stub")
    # Commit the pending edit so `git diff HEAD` is genuinely empty.
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "wip"], cwd=repo, check=True)
    # An explicit range, not bare HEAD: a clean tree with history now falls
    # back to reviewing the whole worktree (see test_snapshot.py), so only a
    # range that truly holds no commits can still be empty.
    result = call(
        "tools/call",
        {"name": "review_diff", "arguments": {"repo": str(repo), "diff": "HEAD..HEAD"}},
    )
    payload = result["result"]
    assert payload["isError"] is False
    assert "没有可审查的变更" in payload["content"][0]["text"]
