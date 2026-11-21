"""Web UI tests — real HTTP against a loopback server, stub model provider.

The point is to exercise the same path the browser hits (routing, JSON, status
writes, artifact reads) without spending tokens or needing a real endpoint.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import threading
from pathlib import Path

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


# --- configuration read / write -------------------------------------------


def test_config_get_masks_api_key(client, monkeypatch):
    monkeypatch.setenv("CODEERRATUM_API_KEY", "sk-super-secret-12345678")
    info = client.get("/api/config").json()
    assert info["source"] == "env"
    assert info["configured"] is True
    assert info["models"][0]["api_key_set"] is True
    assert info["models"][0]["api_key_hint"].endswith("5678")
    assert "sk-super-secret-12345678" not in json.dumps(info)


def test_config_save_roundtrip_and_file_permissions(client, repo):
    resp = client.post(
        "/api/config",
        json={
            "mode": "quick",
            "models": [
                {
                    "model_id": "saved-model",
                    "base_url": "https://api.example.com/v1",
                    "api_key": "sk-abcdefghijkl",
                    "family": "openai",
                }
            ],
        },
    )
    assert resp.status_code == 200
    info = resp.json()
    assert info["source"] == "file"
    assert info["models"][0]["model_id"] == "saved-model"
    assert "sk-abcdefghijkl" not in json.dumps(info)

    path = repo / ".codeerratum" / "config.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text())["models"][0]["api_key"] == "sk-abcdefghijkl"
    assert client.get("/api/meta").json()["source"] == "file"


def test_keep_secret_retains_existing_key(client, repo):
    model = {
        "model_id": "saved-model",
        "base_url": "https://api.example.com/v1",
        "api_key": "sk-original-key-value",
    }
    assert client.post("/api/config", json={"mode": "quick", "models": [model]}).status_code == 200
    resp = client.post(
        "/api/config",
        json={
            "mode": "quick",
            "models": [{**model, "api_key": "__KEEP__"}],
        },
    )
    assert resp.status_code == 200
    stored = json.loads((repo / ".codeerratum" / "config.json").read_text())
    assert stored["models"][0]["api_key"] == "sk-original-key-value"


@pytest.mark.parametrize(
    ("payload", "fragment"),
    [
        (
            {"mode": "bogus", "models": [{"model_id": "x", "base_url": "https://x.test"}]},
            "mode must be",
        ),
        ({"mode": "quick", "models": []}, "models must be"),
        (
            {"mode": "quick", "models": [{"model_id": "x", "base_url": "file:///etc/passwd"}]},
            "base_url must start",
        ),
        (
            {
                "mode": "quick",
                "models": [
                    {"model_id": "dup", "base_url": "https://a.test"},
                    {"model_id": "dup", "base_url": "https://b.test"},
                ],
            },
            "duplicate model_id",
        ),
    ],
)
def test_config_rejects_bad_payload(client, payload, fragment):
    resp = client.post("/api/config", json=payload)
    assert resp.status_code == 400
    assert fragment in resp.json()["error"]


def test_config_post_requires_same_origin(client):
    resp = client.post(
        "/api/config",
        json={"mode": "quick", "models": [{"model_id": "x", "base_url": "https://x.test"}]},
        headers={"Origin": "http://evil.example"},
    )
    assert resp.status_code == 403


def test_saved_config_drives_the_review(client, monkeypatch):
    """A browser edit must change which model actually runs, not just a file."""
    resp = client.post(
        "/api/config",
        json={
            "mode": "quick",
            "models": [
                {
                    "model_id": "from-file",
                    "base_url": "http://stub.invalid/v1",
                    "family": "openai",
                }
            ],
        },
    )
    assert resp.status_code == 200
    # If the pipeline fell back to env, this stale value would show up instead.
    monkeypatch.setenv("CODEERRATUM_MODEL", "env-should-be-ignored")
    summary = client.post("/api/review", json={"diff": "HEAD", "mode": "quick"})
    assert summary.status_code == 200, summary.text
    runs = client.get("/api/runs").json()["runs"]
    assert runs[0]["model_id"] == "from-file"


def test_corrupt_config_file_is_not_silently_ignored(client, repo):
    (repo / ".codeerratum").mkdir(exist_ok=True)
    (repo / ".codeerratum" / "config.json").write_text("{ not json", encoding="utf-8")
    info = client.get("/api/config").json()
    assert "cannot read" in info["config_error"]
    resp = client.post("/api/review", json={"diff": "HEAD~1"})
    assert resp.status_code == 400
    assert "cannot read" in resp.json()["error"]


def _git_repo(path, name: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for args in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@t.io"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(args, cwd=path, check=True, capture_output=True)
    (path / name).write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True, capture_output=True)
    return path


def test_switch_repo_http_changes_target(client, tmp_path):
    """The folder a user pastes becomes the review target for subsequent calls."""
    other = _git_repo(tmp_path / "other", "m.py")
    (other / "src").mkdir()
    # A subdirectory snaps up to the git root, as `ui --repo` does.
    resp = client.post("/api/repo", json={"path": str(other / "src")})
    assert resp.status_code == 200, resp.text
    assert resp.json()["repo"] == str(other.resolve())
    assert client.get("/api/meta").json()["repo"] == str(other.resolve())
    assert client.get("/api/runs").json()["runs"] == []


def test_switch_repo_reads_the_target_config(client, tmp_path):
    other = _git_repo(tmp_path / "other", "m.py")
    (other / ".codeerratum").mkdir()
    (other / ".codeerratum" / "config.json").write_text(
        json.dumps(
            {
                "mode": "quick",
                "models": [
                    {
                        "model_id": "other-repo-model",
                        "base_url": "http://stub.invalid/v1",
                        "family": "openai",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    info = client.post("/api/repo", json={"path": str(other)}).json()
    assert info["configured"]
    assert [m["model_id"] for m in info["models"]] == ["other-repo-model"]


@pytest.mark.parametrize(
    ("path", "fragment"),
    [
        ("", "请填写"),
        ("relative/dir", "绝对路径"),
        ("/nonexistent/path/for/tests", "路径不存在"),
    ],
)
def test_switch_repo_rejects_bad_paths(client, path, fragment):
    resp = client.post("/api/repo", json={"path": path})
    assert resp.status_code == 400
    assert fragment in resp.json()["error"]


def test_switch_repo_requires_git(client):
    """A plain folder with no .git anywhere above it is refused."""
    with tempfile.TemporaryDirectory() as plain:
        resp = client.post("/api/repo", json={"path": plain})
    assert resp.status_code == 400
    assert "不是 git 仓库" in resp.json()["error"]


def test_switch_repo_post_requires_same_origin(client, tmp_path):
    other = _git_repo(tmp_path / "other", "m.py")
    resp = client.post(
        "/api/repo",
        json={"path": str(other)},
        headers={"Origin": "http://evil.example"},
    )
    assert resp.status_code == 403
    assert client.get("/api/meta").json()["repo"] != str(other.resolve())


def test_switch_repo_refused_during_review(client, repo, tmp_path):
    """Switching mid-review would write artifacts into the wrong repository."""
    other = _git_repo(tmp_path / "other", "m.py")
    # The HTTP client cannot expose the server's WebApp; exercise the same
    # method directly with its lock held, which is the state a running review
    # creates. `client` is still requested so the same app fixture stays warm.
    app = WebApp(repo)
    assert app._review_lock.acquire(blocking=False)
    try:
        with pytest.raises(RuntimeError, match="审查进行中"):
            app.switch_repo(str(other))
    finally:
        app._review_lock.release()
    assert app.repo_root == repo.resolve()
