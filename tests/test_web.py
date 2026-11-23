"""Web UI tests — real HTTP against a loopback server, stub model provider.

The point is to exercise the same path the browser hits (routing, JSON, status
writes, artifact reads) without spending tokens or needing a real endpoint.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
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
        c.app = app  # let tests drive the same WebApp the server uses (status/lock)
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


def test_default_out_filename_is_erratum_review_md(client, repo):
    """The work order lands under the product name unless an explicit out is given."""
    summary = client.post("/api/review", json={"diff": "HEAD", "write_markdown": True}).json()
    assert summary["out_path"].endswith("erratumReview.md")
    assert (repo / "erratumReview.md").read_text(encoding="utf-8").startswith("# Code Review 工单")
    assert not (repo / "review.md").exists()


def test_empty_diff_is_reported_without_error_status(client, repo):
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "apply"], cwd=repo, check=True)
    # HEAD..HEAD rather than HEAD: bare HEAD on a clean tree reviews everything
    # now, which would try to reach a model. An empty range still answers
    # "no changes" without failing the request.
    resp = client.post("/api/review", json={"diff": "HEAD..HEAD"})
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


def test_get_run_snapshots_repo_and_store_before_a_concurrent_switch(repo, tmp_path, monkeypatch):
    """switch_repo only holds the review lock; a read in flight must not graft
    another project's run rows/statuses onto this project's artifacts."""
    from codeerratum.web import server as web_server

    run_id = "aabbccddeeff"
    run_dir = repo / ".codeerratum" / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "findings.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "repo": str(repo),
                "commit": "c1",
                "mode": "quick",
                "findings": [{"id": "abc123abc123abcd"}],
            }
        ),
        encoding="utf-8",
    )
    collection = FindingCollection(
        run_id=run_id,
        repo=str(repo),
        commit="c1",
        model_id="gpt-stub",
        spec_level="L1",
        findings=[],
    )
    store = ReviewStore(repo / ".codeerratum" / "feedback.db")
    store.save_run(collection, "2026-01-01T00:00:00Z")
    with store._conn() as conn:  # noqa: SLF001 - test seam by design
        conn.execute(
            "INSERT INTO findings (id, run_id, commit_sha, severity, claim, file, category, "
            "status, created_at) VALUES ('abc123abc123abcd',?,'c1','P1','x','m.py',"
            "'boundary','rejected','2026-01-02T00:00:00Z')",
            (run_id,),
        )

    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=other, check=True)

    app = WebApp(repo)
    real_store_for = web_server.store_for
    switched: list[bool] = []

    def racing_store_for(root):
        result = real_store_for(root)
        if not switched:
            switched.append(True)
            # A concurrent POST /api/repo lands right after the first store
            # access; every later read must still use the original project.
            app.repo_root = other
        return result

    monkeypatch.setattr(web_server, "store_for", racing_store_for)
    detail = app.get_run(run_id)
    assert detail["created_at"] == "2026-01-01T00:00:00Z"
    assert detail["zones"]["all"][0]["status"] == "rejected"
    assert detail["repo"] == str(repo)


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


def test_pipeline_refuses_an_unknown_revision_in_a_plain_folder(tmp_path):
    """No repository means no revisions, but it does not mean "nothing to review".

    ``HEAD``-style ranges fall back to a snapshot (covered in test_snapshot.py);
    an explicit revision cannot, and that is the only remaining refusal.
    """
    from codeerratum.pipeline import NotARepository, execute_review

    with pytest.raises(NotARepository, match="WORKTREE"):
        execute_review(tmp_path, diff_spec="main")


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


# The folder picker and the file-manager opener each branch per platform. The
# tests below pin that branch explicitly instead of inheriting the machine's
# platform: a suite that only passes on macOS says nothing about the Linux
# path, and CI runs on ubuntu. `shutil.which` is faked as well — the Linux
# branch looks for zenity / xdg-open, and a headless CI box has neither.
PLATFORMS = ["darwin", "win32", "linux"]


def _pretend_platform(monkeypatch, platform: str) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")


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


def test_switch_repo_accepts_a_plain_folder(client):
    """A folder with no .git is switchable: the pipeline snapshots it instead.

    Requiring a repository here was the wall behind "my new project is not
    committed, so I cannot review it at all".
    """
    with tempfile.TemporaryDirectory() as plain:
        resp = client.post("/api/repo", json={"path": plain})
    assert resp.status_code == 200, resp.text


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


def test_find_repo_by_name_from_recent_hints(client, tmp_path):
    """A dropped folder name resolves via recently used repo hints."""
    other = _git_repo(tmp_path / "projects" / "dropped-repo", "m.py")
    body = {"name": "dropped-repo", "hints": [str(tmp_path / "projects")]}
    found = client.post("/api/find-repo", json=body).json()
    assert str(other.resolve()) in found["matches"]


def test_find_repo_by_name_rejects_path_like_names(client):
    resp = client.post("/api/find-repo", json={"name": "../../etc"})
    assert resp.status_code == 400
    assert "无效" in resp.json()["error"]


def test_find_repo_by_name_ignores_plain_folders(client, tmp_path):
    (tmp_path / "not-a-repo").mkdir()
    found = client.post(
        "/api/find-repo", json={"name": "not-a-repo", "hints": [str(tmp_path)]}
    ).json()
    assert found["matches"] == []


@pytest.mark.parametrize("platform", PLATFORMS)
def test_pick_repo_returns_git_root(client, monkeypatch, platform):
    """The native dialog's chosen path is snapped to its git root."""
    import subprocess as sp

    _pretend_platform(monkeypatch, platform)
    repo_alt = _git_repo(
        Path(tempfile.mkdtemp(prefix="ce-pick-")) / "proj",
        "m.py",
    )
    (repo_alt / "src").mkdir()

    class Done:
        returncode = 0
        stdout = str(repo_alt / "src") + "\n"
        stderr = ""

    monkeypatch.setattr(sp, "run", lambda *a, **k: Done())
    app = WebApp(repo_alt)
    result = app.pick_repo()
    assert result["path"] == str(repo_alt.resolve())


@pytest.mark.parametrize("platform", PLATFORMS)
def test_pick_repo_cancel_is_clean(client, monkeypatch, platform):
    import subprocess as sp

    _pretend_platform(monkeypatch, platform)

    class Canceled:
        returncode = 1
        stdout = ""
        stderr = "User canceled."

    monkeypatch.setattr(sp, "run", lambda *a, **k: Canceled())
    app = WebApp(Path(client.get("/api/meta").json()["repo"]))
    with pytest.raises(Exception, match="取消"):
        app.pick_repo()


def test_pick_repo_without_zenity_names_the_missing_tool(monkeypatch):
    """A Linux box with no zenity must say which tool is missing."""
    import codeerratum.web.server as web_server

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(Exception, match="zenity"):
        web_server.pick_folder("t")


def test_pick_and_find_require_same_origin(client):
    for path in ("/api/pick-repo", "/api/find-repo"):
        resp = client.post(path, json={}, headers={"Origin": "http://evil.example"})
        assert resp.status_code == 403


@pytest.mark.parametrize(
    "platform,opener",
    [("darwin", "open"), ("win32", "explorer"), ("linux", "/usr/bin/xdg-open")],
)
def test_open_repo_folder_uses_server_side_repo_root(client, repo, monkeypatch, platform, opener):
    """The button reveals the current repo, never a path supplied by the page."""
    import codeerratum.web.server as web_server

    _pretend_platform(monkeypatch, platform)
    calls = []

    class Done:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(web_server.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or Done())
    resp = client.post("/api/repo/open", json={"path": "/etc"})
    assert resp.status_code == 200
    assert resp.json() == {"path": str(repo.resolve()), "opened": True}
    assert calls and calls[0][0] == opener
    assert calls[0][-1] == str(repo.resolve())


@pytest.mark.parametrize(
    "platform,reports_failure",
    [("darwin", True), ("win32", False), ("linux", True)],
)
def test_open_repo_folder_reports_failure_in_chinese(
    client, monkeypatch, platform, reports_failure
):
    import codeerratum.web.server as web_server

    _pretend_platform(monkeypatch, platform)

    class Failed:
        returncode = 1
        stdout = ""
        stderr = "open: no such file\n"

    monkeypatch.setattr(web_server.subprocess, "run", lambda *a, **k: Failed())
    resp = client.post("/api/repo/open", json={})
    if not reports_failure:
        # Explorer reports a non-zero status after it has opened the window.
        # There is nothing the user can act on, so it is not treated as a
        # failure — only the other two platforms surface the error.
        assert resp.status_code == 200
        return
    assert resp.status_code == 400
    assert "无法打开当前仓库文件夹" in resp.json()["error"]


def test_open_repo_folder_requires_same_origin(client):
    resp = client.post("/api/repo/open", json={}, headers={"Origin": "http://evil.example"})
    assert resp.status_code == 403


def test_ui_exposes_open_current_repo_folder_button():
    from codeerratum.web.server import STATIC_DIR

    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="repoOpen"' in html
    assert "打开当前仓库文件夹" in html
    assert '"/api/repo/open"' in html
    assert '("repoOpen").onclick = openRepoFolder' in html


def test_model_check_route_ok(client, monkeypatch):
    """The browser's probe call returns a green-tick payload over HTTP."""
    import codeerratum.web.server as web_server

    monkeypatch.setattr(
        web_server,
        "probe_model",
        lambda **kw: {"ok": True, "status": 200, "latency_ms": 12, "detail": "可用 · 12ms"},
    )
    resp = client.post(
        "/api/model/check",
        json={"model_id": "m", "base_url": "https://x.test", "api_key": ""},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_model_check_requires_same_origin(client):
    resp = client.post(
        "/api/model/check",
        json={"model_id": "m", "base_url": "https://x.test"},
        headers={"Origin": "http://evil.example"},
    )
    assert resp.status_code == 403


def test_errors_use_modal_dialog_not_auto_dismissing_toast():
    """Errors must stay on screen until clicked and be selectable for copying."""
    import re

    from codeerratum.web.server import STATIC_DIR

    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for marker in ('id="errOverlay"', 'id="errText"', 'id="errCopy"', 'id="errClose"'):
        assert marker in html
    assert "function showError(" in html
    assert "审查失败" in html
    # No error path may still route through the disappearing toast.
    assert not re.search(r"toast\([^;]*?,\s*true\s*\)", html)
    # The default must not be a revision that a one-commit repository lacks.
    assert 'id="diff" value="HEAD"' in html


def test_ui_defaults_and_mutually_exclusive_drawers():
    """Writing the work order is opt-out; the four panels never stack."""
    from codeerratum.web.server import STATIC_DIR

    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="writeMd" checked' in html
    assert "同时写 erratumReview.md 到仓库根" in html
    assert "function showOnlyDrawer(" in html
    assert "function toggleDrawer(" in html
    assert '$(btn).classList.toggle("active", id === openId)' in html
    assert 'document.addEventListener("click", (ev)' in html  # click-away closes
    for drawer, btn in (
        ("drawer", "statsBtn"),
        ("docsDrawer", "docsBtn"),
        ("repoDrawer", "repoBtn"),
        ("configDrawer", "configBtn"),
    ):
        assert f'{drawer}: "{btn}"' in html
        assert f'toggleDrawer("{drawer}")' in html


def test_ui_has_draggable_splitter_and_sticky_progress():
    """Sidebar/content split is resizable; progress stays visible while scrolling."""
    from codeerratum.web.server import STATIC_DIR

    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="splitter"' in html
    assert 'role="separator"' in html
    assert "grid-template-columns: var(--sidebar-w" in html
    assert "function setupSplitter" in html
    assert "pointermove" in html
    assert "--sidebar-w" in html and "saveLayout({ sidebarW" in html
    # Progress panel sticky + large, user-resizable log area.
    assert "position: sticky; top: 0" in html
    assert 'id="logResize"' in html
    assert "height: var(--progress-h" in html
    assert "function setupLogResize" in html


def test_review_stream_pushes_progress_then_done(client):
    """The SSE route streams log frames during the review and ends with a summary."""
    with client.stream("POST", "/api/review/stream", json={"diff": "HEAD", "max_units": 5}) as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        events = []
        buf = ""
        for chunk in resp.iter_text():
            buf += chunk
            while "\n\n" in buf:
                frame, buf = buf.split("\n\n", 1)
                data = next((ln[5:] for ln in frame.splitlines() if ln.startswith("data:")), None)
                if data:
                    events.append(json.loads(data))

    kinds = [e["type"] for e in events]
    assert "log" in kinds
    assert kinds[-1] == "done", kinds
    assert any("审查单元" in e.get("message", "") for e in events if e["type"] == "log")
    # Findings travel as their own frames, and they arrive before the run ends:
    # a panel that only learns the total at "done" shows nothing for minutes.
    assert "finding" in kinds, kinds
    assert kinds.index("finding") < kinds.index("done")
    brief = next(e["finding"] for e in events if e["type"] == "finding")
    assert brief["claim"] and brief["file"] and brief["model"]
    # detail travels too: a one-line claim alone reads as "less detailed than
    # the old report" — the reasoning is what makes a live card judgeable.
    assert brief.get("detail"), "the live card needs the reasoning, not just the headline"
    done = events[-1]
    assert done["findings"] == 1
    assert done["run_id"]


def test_review_stream_validation_error_is_json_before_headers(client):
    """Bad input is a real 400, not an HTTP 200 with an error event."""
    resp = client.post("/api/review/stream", json={"diff": "HEAD", "max_units": "not-an-int"})
    assert resp.status_code == 400
    assert "max_units" in resp.json()["error"]


def test_review_stream_requires_same_origin(client):
    resp = client.post(
        "/api/review/stream",
        json={"diff": "HEAD"},
        headers={"Origin": "http://evil.example"},
    )
    assert resp.status_code == 403


def test_ui_consumes_review_sse_stream():
    from codeerratum.web.server import STATIC_DIR

    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'fetch(BASE + "/api/review/stream"' in html
    assert 'id="progressLog"' in html
    assert 'ev.type === "done"' in html
    assert 'ev.type === "error"' in html
    assert "getReader()" in html


def test_review_status_reflects_running_review(client):
    """The shared status endpoint exposes in-flight state and progress to all tabs."""
    idle = client.get("/api/review/status").json()
    assert idle["running"] is False
    assert idle["status"] == "idle"

    app = client.app
    app.acquire_review_lock(
        {"diff_spec": "HEAD", "mode": "dual", "max_units": 5, "paths": "src/api"}
    )
    try:
        app._note_progress("[m1] 单元 1/2 完成（1 条发现）")
        st = client.get("/api/review/status").json()
        assert st["running"] is True
        assert st["status"] == "running"
        assert st["params"]["diff"] == "HEAD"
        assert st["params"]["paths"] == "src/api"
        assert st["messages"] == ["[m1] 单元 1/2 完成（1 条发现）"]
        assert st["elapsed_s"] is not None
        assert st["started_at"]  # readable wall-clock start

        # A second review is refused with a Chinese message and a real 409.
        resp = client.post("/api/review/stream", json={"diff": "HEAD"})
        assert resp.status_code == 409
        assert "已有审查" in resp.json()["error"]
    finally:
        app.release_review_lock(status="done", run_id="abc123")

    done = client.get("/api/review/status").json()
    assert done["running"] is False
    assert done["status"] == "done"
    assert done["run_id"] == "abc123"


def test_ui_tracks_external_review_and_busy_guard():
    from codeerratum.web.server import STATIC_DIR

    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'api("/api/review/status")' in html  # polls shared status
    assert "setInterval(pollReviewStatus" in html
    assert "if (preflight && preflight.running)" in html  # second tab guard
    assert "res.status === 409" in html  # race -> track, not error modal
    assert "reviewStreaming = true" in html


def test_token_totals_rolls_up_usage_by_model():
    from codeerratum.pipeline import ReviewOutcome

    outcome = ReviewOutcome(
        run_id="r",
        run_dir=Path("/tmp"),
        collection=FindingCollection(
            run_id="r", repo="x", commit="", model_id="m1,m2", spec_level="L1"
        ),
        units_total=3,
        units_selected=3,
        calls=[
            {
                "model": "m1",
                "usage": {"input_tokens": 100, "output_tokens": 20, "cached_tokens": 10},
            },
            {"model": "m1", "usage": {"input_tokens": 50, "output_tokens": 30, "cached_tokens": 0}},
            {"model": "m2", "usage": {"input_tokens": 10, "output_tokens": 5, "cached_tokens": 0}},
        ],
    )
    tokens = outcome.token_totals()
    assert (tokens["input"], tokens["output"], tokens["cached"], tokens["total"]) == (
        160,
        55,
        10,
        215,
    )
    assert tokens["by_model"]["m1"] == {
        "input": 150,
        "output": 50,
        "cached": 10,
        "total": 200,
        "calls": 2,
    }
    assert tokens["by_model"]["m2"]["total"] == 15
    assert outcome.summary()["tokens"] == tokens


def test_stop_review_signals_running_review_and_tracks_state(client):
    app = client.app
    app.acquire_review_lock({"diff_spec": "HEAD", "mode": "dual", "max_units": 5})
    try:
        assert not app._stop_event.is_set()
        resp = client.post("/api/review/stop", json={})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert app._stop_event.is_set()

        st = client.get("/api/review/status").json()
        assert st["running"] is True
        assert st["status"] == "stopping"
        assert any("已请求停止" in m for m in st["messages"])

        # Repeated clicks are idempotent — one message, no duplicate state churn.
        client.post("/api/review/stop", json={})
        st = client.get("/api/review/status").json()
        assert sum("已请求停止" in m for m in st["messages"]) == 1
    finally:
        app.release_review_lock(status="cancelled")

    st = client.get("/api/review/status").json()
    assert st["status"] == "cancelled"
    assert st["running"] is False
    # Stopping with nothing running is a real 409, not a silent success.
    assert client.post("/api/review/stop", json={}).status_code == 409


def test_review_stream_emits_cancelled_frame(client, monkeypatch):
    from codeerratum.review import ReviewCancelled

    def boom(*_args, **_kwargs):
        raise ReviewCancelled("审查已被用户停止")

    monkeypatch.setattr(client.app, "execute_prepared", boom)
    with client.stream("POST", "/api/review/stream", json={"diff": "HEAD"}) as resp:
        assert resp.status_code == 200
        body = resp.read().decode("utf-8")
    assert "event: cancelled" in body
    assert "审查已被用户停止" in body
    assert "event: error" not in body
    assert client.get("/api/review/status").json()["status"] == "cancelled"


def test_review_stream_emits_stopped_frame_and_keeps_partial_run(client, monkeypatch):
    summary = {
        "run_id": "partial-run",
        "stopped": True,
        "findings": 1,
        "calls": 2,
        "latency_ms_avg": 10,
        "tokens": {"input": 3, "output": 4, "cached": 0, "total": 7, "by_model": {}},
        "comparison": {"stopped": {"units_completed": 1, "units_total": 3}},
    }
    monkeypatch.setattr(client.app, "execute_prepared", lambda *_a, **_k: summary)
    with client.stream("POST", "/api/review/stream", json={"diff": "HEAD"}) as resp:
        assert resp.status_code == 200
        body = resp.read().decode("utf-8")
    assert "event: stopped" in body
    assert "event: done" not in body
    st = client.get("/api/review/status").json()
    assert st["status"] == "stopped"
    assert st["running"] is False
    assert st["run_id"] == "partial-run"
    assert st["tokens"]["total"] == 7


def test_run_review_cancels_inflight_model_calls(monkeypatch, tmp_path):
    """The stop flag reaches inside asyncio and cancels a call already in flight."""
    import asyncio
    import threading

    import codeerratum.review.orchestrator as orch
    from codeerratum.review import ReviewCancelled

    class Spec:
        model_id = "slow-model"

    class Registry:
        def route(self, mode, author_model=None, pinned=None):
            return [Spec()], None

    async def slow_review(*_args, **_kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr(orch, "_make_provider", lambda _spec: object())
    monkeypatch.setattr(orch, "_review_with", slow_review)
    stop = threading.Event()

    async def scenario():
        task = asyncio.create_task(
            orch.run_review(Registry(), "quick", [], tmp_path, "r", should_stop=stop.is_set)
        )
        await asyncio.sleep(0.4)
        stop.set()
        try:
            await asyncio.wait_for(task, timeout=3)
        except ReviewCancelled:
            return True
        return False

    assert asyncio.run(scenario()) is True


def test_ui_exposes_stop_button_and_token_totals():
    from codeerratum.web.server import STATIC_DIR

    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="stopBtn"' in html
    assert 'api("/api/review/stop"' in html
    assert 'ev.type === "cancelled"' in html
    assert 'st.status === "cancelled"' in html
    assert 'ev.type === "stopped"' in html
    assert 'st.status === "stopped"' in html
    assert "tokenSummary(ev.tokens)" in html
    assert "token 消耗" in html


def test_review_error_survives_in_status(client):
    """A failed run must keep its reason after the SSE socket is gone."""
    app = client.app
    app.acquire_review_lock({"diff_spec": "HEAD", "mode": "dual", "max_units": 1, "paths": ""})
    app.release_review_lock(status="error", error="证伪调用失败：ReadTimeout")
    st = client.get("/api/review/status").json()
    assert st["running"] is False
    assert st["status"] == "error"
    assert st["error"] == "证伪调用失败：ReadTimeout"
    assert len(st["finished_at"]) == 8  # readable wall clock, not monotonic seconds


def test_safe_error_redacts_local_paths():
    from codeerratum.web.server import _safe_error

    repo = Path("/tmp/my-secret-project")
    msg = RuntimeError(f"cannot open {repo}/.codeerratum/config.json after 12s\nsecond line")
    text = _safe_error(msg, repo)
    assert "/tmp/my-secret-project" not in text
    assert "<repo>/.codeerratum/config.json" in text
    assert "\n" not in text


def test_sse_log_levels_are_coloured_in_page(client):
    html = client.get("/").text
    assert "function logLevel" in html
    assert "#progressLog .ln.err { color: var(--bad)" in html
    assert "#progressLog .ln.warn { color: var(--warn)" in html
    assert "st.error" in html  # other tabs see the failure reason too


class _GoneWriter:
    """Stands in for a socket whose browser tab was closed."""

    def write(self, _data):  # noqa: ANN001
        raise BrokenPipeError("client gone")

    def flush(self) -> None:
        pass


def test_sse_write_reports_disconnected_client_instead_of_raising():
    from codeerratum.web.server import _write_sse

    assert _write_sse(_GoneWriter(), "log", {"message": "x"}) is False

    buf = io.BytesIO()
    assert _write_sse(buf, "log", {"message": "审查中"}) is True
    body = buf.getvalue().decode()
    assert body.startswith("event: log\ndata: ")
    assert '"type": "log"' in body


def test_stream_handler_survives_closed_tab(client):
    """The disconnect guard must be wired in, not just defined."""
    import inspect

    from codeerratum.web import server as web_server

    src = inspect.getsource(web_server.make_handler)
    assert "_client_gone" in src
    assert "_write_sse" in src


def test_models_endpoint_lists_choices_without_keys(client):
    """The picker needs ids to offer; it must never receive the api_key."""
    resp = client.get("/api/models")
    assert resp.status_code == 200
    body = resp.json()
    assert body["seats"] == {"quick": 1, "dual": 2, "committee": 3}
    assert body["auto_budget"] >= 1
    assert "gpt-stub" in [row["id"] for row in body["models"]]
    assert "api_key" not in resp.text and "sk-" not in resp.text


def test_unknown_model_selection_is_a_400_before_the_run_starts(client):
    resp = client.post("/api/review", json={"diff": "HEAD", "models": ["gpt-nope"]})
    assert resp.status_code == 400
    assert "gpt-nope" in resp.json()["error"]


def test_out_of_range_max_units_is_clamped_with_a_notice(client):
    """Above the cap the run is clamped, and it says so instead of doing it quietly."""
    params = client.app.prepare_review({"diff": "HEAD", "max_units": 5000})
    assert params["max_units"] == 500
    assert params["notices"] == ["上限 5000 超出范围（最多 500 个审查单元），已按 500 执行"]

    summary = client.post("/api/review", json={"diff": "HEAD", "max_units": 5000}).json()
    assert summary["notices"] == params["notices"]
    # Other tabs read the same notice from the shared status, not just the initiator.
    assert client.get("/api/review/status").json()["params"]["notices"] == params["notices"]


def test_max_units_zero_means_every_unit(client):
    """0 is the documented spelling of "no budget", not an out-of-range value.

    Coercing it to 1 (the old behaviour) made the one setting people need for a
    full sweep impossible to express from the UI.
    """
    params = client.app.prepare_review({"diff": "HEAD", "max_units": 0})
    assert params["max_units"] == 0
    assert params["notices"] == []

    # A negative number is still nonsense and still gets clamped.
    negative = client.app.prepare_review({"diff": "HEAD", "max_units": -3})
    assert negative["max_units"] == 1
    assert "至少 1 个" in negative["notices"][0]


def test_in_range_max_units_carries_no_notice(client):
    params = client.app.prepare_review({"diff": "HEAD", "max_units": 10})
    assert params["max_units"] == 10
    assert params["notices"] == []
    summary = client.post("/api/review", json={"diff": "HEAD", "max_units": 10}).json()
    assert "notices" not in summary


def test_clamp_notice_reaches_progress_log(client):
    """The clamp must appear in the SSE/status log, before any unit is reviewed."""
    messages: list[str] = []
    params = client.app.prepare_review({"diff": "HEAD", "max_units": 999})
    client.app.acquire_review_lock(params)
    try:
        client.app.execute_prepared(params, progress=messages.append)
    finally:
        client.app.release_review_lock()
    assert messages and messages[0].startswith("上限 999 超出范围")
    assert any("上限 999" in m for m in client.get("/api/review/status").json()["messages"])


def test_units_hint_mirrors_the_cap_in_the_page():
    from codeerratum.web.server import STATIC_DIR

    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="maxUnitsHint"' in html
    assert "function syncUnitsHint" in html
    assert "const MAX_UNITS_CAP = 500;" in html
    assert ".hint.warn { color: var(--warn)" in html
    assert "max_units: unitsValue()," in html  # 0 must not become 10


def test_language_header_switches_repo_errors(client):
    """The same failure is worded in the language the tab asked for."""
    en = client.post(
        "/api/repo", json={"path": "/nope/definitely-missing"}, headers={"X-Lang": "en"}
    )
    assert en.status_code == 400
    assert "does not exist" in en.json()["error"]

    zh = client.post("/api/repo", json={"path": "/nope/definitely-missing"})
    assert "路径不存在" in zh.json()["error"]


def test_language_header_switches_run_notices(client):
    # 5000 is above the 500 cap, so this run produces a clamp notice in whichever
    # language the tab asked for.
    en = client.post(
        "/api/review", json={"diff": "HEAD", "max_units": 5000}, headers={"X-Lang": "en"}
    )
    assert "Limit 5000" in en.json()["notices"][0]

    zh = client.post("/api/review", json={"diff": "HEAD", "max_units": 5000})
    assert "已按 500 执行" in zh.json()["notices"][0]


def test_language_header_switches_lock_messages(client):
    en = client.post("/api/review/stop", json={}, headers={"X-Lang": "en"})
    assert en.status_code == 409
    assert "No review is running" in en.json()["error"]

    zh = client.post("/api/review/stop", json={})
    assert "当前没有正在进行的审查" in zh.json()["error"]


def test_pages_expose_a_language_switch():
    """Both pages must carry the selector and the translation helpers."""
    from codeerratum.web.server import STATIC_DIR

    for name in ("index.html", "projects.html"):
        html = (STATIC_DIR / name).read_text(encoding="utf-8")
        assert 'id="langSel"' in html, name
        assert "codeerratum.lang" in html, name
        assert "function T(" in html, name
        assert 'value="en">English' in html, name


def test_meta_carries_version_and_homepage(client):
    """The About dialog reads both from /api/meta instead of hard-coding them."""
    import codeerratum

    data = client.get("/api/meta").json()
    assert data["version"] == codeerratum.__version__
    assert data["homepage"] == codeerratum.HOMEPAGE
    assert data["homepage"].startswith("https://github.com/")


def test_open_homepage_uses_the_constant_not_the_request(client, monkeypatch):
    """A page must not be able to aim this at an arbitrary URL."""
    import codeerratum.web.server as web_server

    opened: list[str] = []
    monkeypatch.setattr(
        web_server.webbrowser, "open", lambda url, new=0: opened.append(url) or True
    )

    resp = client.post("/api/open/homepage", json={"url": "http://evil.example"})
    assert resp.status_code == 200
    assert resp.json()["url"] == web_server.HOMEPAGE
    assert opened == [web_server.HOMEPAGE]


def test_open_homepage_is_same_origin_only(client):
    resp = client.post("/api/open/homepage", json={}, headers={"Origin": "http://evil.example"})
    assert resp.status_code == 403


def test_about_dialog_and_shell_entry_point():
    """The packaged app's About panel cannot carry a link; these can."""
    from codeerratum.web.server import STATIC_DIR

    review = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    shell = (STATIC_DIR / "projects.html").read_text(encoding="utf-8")
    assert 'id="aboutBtn"' in review and 'id="aboutOverlay"' in review
    assert 'id="aboutUrl"' in review
    assert 'api("/api/open/homepage"' in review
    assert 'id="githubBtn"' in shell
    assert 'api("/api/open/homepage"' in shell


def test_status_carries_live_findings_for_tabs_that_joined_mid_run(client):
    """The SSE connection belongs to the tab that opened it.

    A refresh, a second tab, or a run started elsewhere polls
    /api/review/status — and used to get progress lines with no findings at all
    until the report was written, which is exactly "右边没显示出来".
    """
    with client.stream("POST", "/api/review/stream", json={"diff": "HEAD"}) as resp:
        for _ in resp.iter_text():
            pass
    status = client.get("/api/review/status").json()
    findings = status.get("findings") or []
    assert findings, "tracking tabs need the findings, not just the log"
    assert findings[0]["claim"] and findings[0]["file"]


def test_new_run_starts_with_an_empty_live_list(client):
    """Otherwise the panel shows the previous run's findings as freshly found."""
    app = client.app
    app._note_finding({"id": "abc", "model": "m", "claim": "stale"})
    assert app.review_status()["findings"]

    app.acquire_review_lock({})
    try:
        assert app.review_status()["findings"] == []
    finally:
        app.release_review_lock()


def test_live_findings_are_deduped_per_model(client):
    """Same claim from two models is two rows, the same model twice is one."""
    app = client.app
    brief = {"id": "f1", "model": "m1", "claim": "c"}
    app._note_finding(brief)
    app._note_finding(dict(brief))
    app._note_finding({"id": "f1", "model": "m2", "claim": "c"})
    assert len(app.review_status()["findings"]) == 2
