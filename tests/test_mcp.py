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
