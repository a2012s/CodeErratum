"""Multi-project hub — isolated project sessions behind one loopback server."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import httpx
import pytest

from codeerratum.pipeline import PipelineError
from codeerratum.web import ProjectHub, make_server
from codeerratum.web.hub import slug_for


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.io"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "a.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)
    return path


def test_slug_is_stable_and_short(tmp_path):
    repo = make_repo(tmp_path / "alpha")
    first = slug_for(repo.resolve())
    assert first == slug_for(repo.resolve())
    assert len(first) == 10 and first.isalnum()
    assert first != slug_for((tmp_path / "beta").resolve())


def test_add_dedupes_and_validates(tmp_path):
    hub = ProjectHub(state_file=tmp_path / "projects.json")
    repo = make_repo(tmp_path / "alpha")

    first = hub.add(str(repo))
    again = hub.add(str(repo / ""))
    assert first["slug"] == again["slug"]
    assert len(hub.snapshot()["projects"]) == 1

    with pytest.raises(PipelineError, match="绝对路径"):
        hub.add("relative/path")
    with pytest.raises(PipelineError, match="不是 git 仓库"):
        hub.add(str(tmp_path))


def test_hub_persists_and_restores_existing_paths(tmp_path):
    state = tmp_path / "projects.json"
    alpha = make_repo(tmp_path / "alpha")
    beta = make_repo(tmp_path / "beta")

    hub = ProjectHub(state_file=state)
    hub.add(str(alpha))
    hub.add(str(beta))
    assert state.exists()

    # beta disappears from disk: restore keeps alpha and skips the dead path.
    (beta / ".git").rename(beta / ".git-away")
    restored = ProjectHub(state_file=state)
    assert restored.restore() == 1
    rows = restored.snapshot()["projects"]
    assert [r["name"] for r in rows] == ["alpha"]


def test_remove_keeps_disk_data(tmp_path):
    hub = ProjectHub(state_file=tmp_path / "projects.json")
    repo = make_repo(tmp_path / "alpha")
    entry = hub.add(str(repo))

    assert hub.remove(entry["slug"]) == {"removed": entry["slug"]}
    assert hub.snapshot()["projects"] == []
    assert (repo / ".git").exists()


@pytest.fixture
def hub_client(tmp_path):
    alpha = make_repo(tmp_path / "alpha")
    beta = make_repo(tmp_path / "beta")
    hub = ProjectHub(state_file=tmp_path / "projects.json", max_parallel=2)
    entries = [hub.add(str(alpha)), hub.add(str(beta))]
    app = hub.get(entries[0]["slug"])
    httpd = make_server(app, port=0, hub=hub)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    with httpx.Client(
        base_url=base, timeout=20, trust_env=False, headers={"Origin": base}
    ) as client:
        client.hub = hub
        client.entries = entries
        client.repos = [alpha, beta]
        yield client
    httpd.shutdown()
    httpd.server_close()


def test_shell_page_and_project_page_are_distinct(hub_client):
    shell = hub_client.get("/")
    assert shell.status_code == 200
    assert "添加项目" in shell.text
    assert 'id="frames"' in shell.text

    slug = hub_client.entries[0]["slug"]
    page = hub_client.get(f"/p/{slug}/")
    assert page.status_code == 200
    assert f'data-base="/p/{slug}"' in page.text
    assert "function logLevel" in page.text  # the unchanged single-project UI

    assert hub_client.get("/p/deadbeef00/").status_code == 404


def test_project_api_is_isolated_per_slug(hub_client):
    alpha, beta = hub_client.entries
    meta_a = hub_client.get(f"/p/{alpha['slug']}/api/meta").json()
    meta_b = hub_client.get(f"/p/{beta['slug']}/api/meta").json()
    assert meta_a["repo"].endswith("/alpha")
    assert meta_b["repo"].endswith("/beta")
    assert meta_a["state_dir"] != meta_b["state_dir"]

    # Root API keeps pointing at the first project for backward compatibility.
    assert hub_client.get("/api/meta").json()["repo"].endswith("/alpha")

    snapshot = hub_client.get("/api/projects").json()
    assert {p["name"] for p in snapshot["projects"]} == {"alpha", "beta"}
    assert snapshot["max_parallel"] == 2


def test_add_and_remove_project_over_http(hub_client, tmp_path):
    gamma = make_repo(tmp_path / "gamma")
    added = hub_client.post("/api/projects", json={"path": str(gamma)})
    assert added.status_code == 200, added.text
    slug = added.json()["slug"]
    assert slug in {p["slug"] for p in hub_client.get("/api/projects").json()["projects"]}

    removed = hub_client.post(f"/api/projects/{slug}/remove", json={})
    assert removed.status_code == 200
    assert slug not in {p["slug"] for p in hub_client.get("/api/projects").json()["projects"]}

    # Non-git path is a real 400, not a 500.
    bad = hub_client.post("/api/projects", json={"path": str(tmp_path)})
    assert bad.status_code == 400
    assert "不是 git 仓库" in bad.json()["error"]


def test_hub_survives_duplicate_add(hub_client):
    alpha = hub_client.repos[0]
    before = len(hub_client.get("/api/projects").json()["projects"])
    again = hub_client.post("/api/projects", json={"path": str(alpha)})
    assert again.status_code == 200
    assert len(hub_client.get("/api/projects").json()["projects"]) == before


def test_queue_waits_for_a_slot_then_runs(tmp_path):
    hub = ProjectHub(state_file=tmp_path / "projects.json", max_parallel=1)
    repo = make_repo(tmp_path / "alpha")
    app = hub.get(hub.add(str(repo))["slug"])

    hub._slots.acquire()  # simulate another project already running
    done = threading.Event()

    def worker() -> None:
        app._wait_for_slot()
        done.set()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    assert not done.wait(0.6)
    assert app.review_status()["status"] == "queued"
    assert "排队中" in app.review_status()["messages"][-1]

    hub._slots.release()
    assert done.wait(3)
    assert app.review_status()["status"] == "running"


def test_stop_while_queued_cancels_without_running(tmp_path):
    from codeerratum.review.orchestrator import ReviewCancelled

    hub = ProjectHub(state_file=tmp_path / "projects.json", max_parallel=1)
    repo = make_repo(tmp_path / "alpha")
    app = hub.get(hub.add(str(repo))["slug"])
    hub._slots.acquire()
    app._stop_event.set()

    with pytest.raises(ReviewCancelled):
        app._wait_for_slot()
    hub._slots.release()


def test_shell_and_project_page_glue():
    """The two static pages must agree on the base prefix and drop hand-off."""
    from codeerratum.web.server import STATIC_DIR

    shell = (STATIC_DIR / "projects.html").read_text(encoding="utf-8")
    assert '"ce:add-project"' in shell  # the shell listens for forwarded drops
    assert "/api/projects" in shell
    assert 'document.querySelectorAll(".card")' not in shell  # cards are rendered from data

    page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'const BASE = document.documentElement.dataset.base || "";' in page
    assert 'fetch(BASE + "/api/review/stream"' in page
    assert 'st.status === "queued"' in page  # a queued project says so instead of "审查中"
    assert "ce:add-project" in page  # drops inside a tab are forwarded to the shell
