"""Web UI tests — real HTTP against a loopback server, stub model provider.

The point is to exercise the same path the browser hits (routing, JSON, status
writes, artifact reads) without spending tokens or needing a real endpoint.
"""

from __future__ import annotations

import json
import subprocess
import threading

import httpx
import pytest

import codeerratum.review.orchestrator as orchestrator
from codeerratum.models import FindingCollection
from codeerratum.providers.base import Completion, Usage
from codeerratum.review.schema import LLMAnchor, LLMFinding, LLMReview, LLMSuggestion
from codeerratum.store import ReviewStore
from codeerratum.web import WebApp, make_server


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
                    detail="items 为空时 IndexError，接口 500。",
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
def repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.io"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "m.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    (tmp_path / "m.py").write_text("def foo():\n    x = []\n    return x[0]\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def client(repo, monkeypatch):
    monkeypatch.setenv("CODEERRATUM_BASE_URL", "http://stub.invalid/v1")
    monkeypatch.setenv("CODEERRATUM_MODEL", "gpt-stub")
    monkeypatch.setenv("CODEERRATUM_MODE", "quick")
    monkeypatch.delenv("CODEERRATUM_MODELS", raising=False)

    source_lines = (repo / "m.py").read_text().splitlines(keepends=True)
    line = next(i for i, text in enumerate(source_lines, 1) if "return x[0]" in text)
    stub = StubProvider(line, source_lines[line - 1])
    monkeypatch.setattr(orchestrator, "_make_provider", lambda spec: stub)

    app = WebApp(repo)
    httpd = make_server(app, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    # A same-origin browser POST always carries Origin. Send it by default so
    # tests exercise that path; explicit security tests override it.
    with httpx.Client(base_url=base, timeout=30, trust_env=False, headers={"Origin": base}) as c:
        yield c
    httpd.shutdown()
    httpd.server_close()


def test_index_and_meta(client, repo):
    index = client.get("/")
    assert index.status_code == 200
    assert "CodeErratum" in index.text
    assert "text/html" in index.headers["content-type"]

    meta = client.get("/api/meta").json()
    assert meta["configured"] is True
    assert meta["repo"] == str(repo)
    assert meta["models"] == [{"model_id": "gpt-stub", "family": "openai"}]


def test_review_then_read_status_and_markdown(client, repo):
    resp = client.post("/api/review", json={"diff": "HEAD", "max_units": 5})
    assert resp.status_code == 200, resp.text
    summary = resp.json()
    assert summary["findings"] == 1
    assert summary["mode"] == "quick"
    assert summary["zones"] == {"all": 1}
    run_id = summary["run_id"]

    runs = client.get("/api/runs").json()["runs"]
    assert [r["run_id"] for r in runs] == [run_id]
    assert runs[0]["has_artifact"] is True

    run = client.get(f"/api/runs/{run_id}").json()
    finding = run["zones"]["all"][0]
    assert finding["claim"] == "空列表取首元素会崩"
    assert finding["status"] == "open"
    assert finding["anchors"][0]["file"] == "m.py"
    assert run["has_markdown"] is True
    assert isinstance(run["notes"], list)

    md = client.get(f"/api/runs/{run_id}/review.md")
    assert md.status_code == 200
    assert "# Code Review 工单" in md.text

    # Human verdict round-trips through the store and shows up on re-read.
    status = client.post(f"/api/findings/{finding['id']}/status", json={"status": "accepted"})
    assert status.status_code == 200
    assert status.json()["status"] == "accepted"
    again = client.get(f"/api/runs/{run_id}").json()["zones"]["all"][0]
    assert again["status"] == "accepted"

    stats = client.get("/api/stats").json()
    assert stats["accepted"] == 1
    assert stats["acceptance_rate"] == 1.0

    # review.md is always archived inside the run dir, never required in the repo root.
    assert (repo / ".codeerratum" / "runs" / run_id / "review.md").exists()
    assert not (repo / "review.md").exists()


def test_write_markdown_flag_copies_to_repo_root(client, repo):
    summary = client.post(
        "/api/review", json={"diff": "HEAD", "write_markdown": True, "out": "review.md"}
    ).json()
    assert summary["out_path"].endswith("review.md")
    assert (repo / "review.md").read_text(encoding="utf-8").startswith("# Code Review 工单")


def test_empty_diff_is_reported_without_error_status(client, repo):
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "apply"], cwd=repo, check=True)
    resp = client.post("/api/review", json={"diff": "HEAD"})
    assert resp.status_code == 200
    assert "No changes" in resp.json()["error"]


def test_bad_inputs_are_rejected(client):
    assert client.get("/api/runs/deadbeefdeadbeef").status_code == 404
    assert client.get("/api/runs/..%2F..%2Fetc").status_code == 404
    bad_status = client.post("/api/findings/0123456789abcdef/status", json={"status": "nonsense"})
    assert bad_status.status_code == 400
    unknown = client.post("/api/findings/0123456789abcdef/status", json={"status": "accepted"})
    assert unknown.status_code == 404


def test_cross_origin_post_is_refused(client, repo):
    resp = client.post(
        "/api/review",
        json={"diff": "HEAD"},
        headers={"Origin": "http://evil.example", "Host": "127.0.0.1"},
    )
    assert resp.status_code == 403
    assert not (repo / ".codeerratum" / "runs").exists()


def test_post_without_origin_is_refused(client, repo):
    """A plain HTML form can send a JSON body as text/plain with no Origin at all.

    Treating 'no Origin' as same-origin let any web page spend the user's tokens.
    """
    resp = client.post("/api/review", json={"diff": "HEAD"}, headers={"Origin": ""})
    assert resp.status_code == 403
    assert not (repo / ".codeerratum" / "runs").exists()


def test_non_loopback_host_is_refused(client, repo):
    """DNS rebinding: evil.com resolving to 127.0.0.1 must not read local state."""
    assert client.get("/api/runs", headers={"Host": "evil.example"}).status_code == 403
    assert client.post("/api/review", json={}, headers={"Host": "evil.example"}).status_code == 403


def test_write_markdown_rejects_paths_outside_repo(client, repo):
    for bad in ("../../escaped.md", "/tmp/escaped.md", "..\\escaped.md"):
        resp = client.post(
            "/api/review",
            json={"diff": "HEAD", "write_markdown": True, "out": bad},
        )
        assert resp.status_code == 400, bad
        assert "out must be a plain filename" in resp.json()["error"]


def test_status_lookup_survives_rerun(repo, monkeypatch):
    """A verdict must attach to the finding id, not to the run that saw it last."""
    collection = FindingCollection(
        run_id="run-a",
        repo=str(repo),
        commit="c1",
        model_id="gpt-stub",
        spec_level="L1",
        findings=[],
    )
    store = ReviewStore(repo / ".codeerratum" / "feedback.db")
    store.save_run(collection, "2026-01-01T00:00:00Z")
    # Insert a finding row directly: the UI only needs the id -> status mapping.
    with store._conn() as conn:  # noqa: SLF001 - test seam by design
        conn.execute(
            "INSERT INTO findings (id, run_id, commit_sha, severity, claim, file, category, "
            "status, created_at) VALUES ('abc123abc123abcd','run-b','c2','P1','x','m.py',"
            "'boundary','rejected','2026-01-02T00:00:00Z')"
        )
    assert store.statuses_for(["abc123abc123abcd"]) == {
        "abc123abc123abcd": {"status": "rejected", "reason": ""}
    }


def test_zone_split_maps_agreement_to_sections():
    from codeerratum.web.server import _split_zones

    findings = [
        {"id": "1", "agreement": "unique", "severity": "P1"},
        {"id": "2", "agreement": "conflict", "severity": "P0"},
        {"id": "3", "agreement": "unanimous", "severity": "P2"},
        {"id": "4", "agreement": "majority", "severity": "P3"},
        {"id": "5", "agreement": "unverified", "severity": "P1"},
    ]
    zones = _split_zones(findings, "dual")
    assert [f["id"] for f in zones["unique"]] == ["1", "5"]
    assert [f["id"] for f in zones["conflict"]] == ["2"]
    assert [f["id"] for f in zones["consensus"]] == ["3", "4"]


def test_pipeline_refuses_non_repository(tmp_path):
    from codeerratum.pipeline import NotARepository, execute_review

    with pytest.raises(NotARepository):
        execute_review(tmp_path)
