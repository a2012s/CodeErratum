"""Pathspec filtering — narrow a review to a folder without staging changes."""

from __future__ import annotations

import json
import subprocess

import httpx
import pytest

import codeerratum.review.orchestrator as orchestrator
from codeerratum.ingest import DiffError, get_diff, normalize_pathspec, split_diff
from codeerratum.pipeline import execute_review
from codeerratum.providers.base import Completion, Usage
from codeerratum.review.schema import LLMReview
from codeerratum.web import WebApp, make_server


def _init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.io"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    for folder in ("src", "tests"):
        (path / folder).mkdir()
        (path / folder / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)
    # Dirty both folders the same way; the pathspec must separate them.
    for folder in ("src", "tests"):
        (path / folder / "m.py").write_text("def f():\n    return 1 + 1\n", encoding="utf-8")
    return path


def test_normalize_pathspec_rejects_escape_and_absolute():
    """Reverting: an absolute path or .. leaks into git and can review files outside the repo."""
    assert normalize_pathspec("  src/api  ") == "src/api"
    assert normalize_pathspec("src/api tests/unit") == "src/api tests/unit"
    for bad in ("/etc", "..\\secret", "src/../tests", "../outside"):
        with pytest.raises(DiffError):
            normalize_pathspec(bad)


def test_get_diff_with_pathspec_excludes_other_folders(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    everything = get_diff("HEAD", repo)
    assert "src/m.py" in everything and "tests/m.py" in everything

    only_src = get_diff("HEAD", repo, paths="src")
    assert "src/m.py" in only_src
    assert "tests/m.py" not in only_src

    both = get_diff("HEAD", repo, paths="src tests")
    assert "src/m.py" in both and "tests/m.py" in both


def test_single_commit_repo_explains_head_tilde(tmp_path):
    """Reverting: the UI's old HEAD~1 default dies with an English git fatal on a 1-commit repo."""
    repo = _init_repo(tmp_path / "repo")
    with pytest.raises(DiffError) as exc:
        get_diff("HEAD~1", repo)
    msg = str(exc.value)
    assert "只有一个提交" in msg
    assert "请填 HEAD" in msg
    assert "git 原始错误" in msg  # keep the copyable raw reason


def test_split_diff_respects_pathspec(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    units = split_diff(get_diff("HEAD", repo, paths="src"), repo)
    assert units
    assert all(u.primary_file.startswith("src/") for u in units)


class StubProvider:
    family = "stub"

    async def complete(self, model_id, messages, schema, temperature=0.0, max_tokens=2048):
        return Completion(
            text=json.dumps(LLMReview(summary="stub", findings=[]).model_dump(mode="json")),
            parsed=LLMReview(summary="stub", findings=[]),
            usage=Usage(input_tokens=1, output_tokens=1),
            raw_request_hash="stub",
            latency_ms=1,
        )


def test_pipeline_notice_records_the_pathspec(tmp_path, monkeypatch):
    """Reverting: a filtered run looks identical to a full run in the report."""
    monkeypatch.setenv("CODEERRATUM_BASE_URL", "http://stub.invalid/v1")
    monkeypatch.setenv("CODEERRATUM_MODEL", "gpt-stub")
    monkeypatch.delenv("CODEERRATUM_MODELS", raising=False)
    monkeypatch.setattr(orchestrator, "_make_provider", lambda spec: StubProvider())
    repo = _init_repo(tmp_path / "repo")

    outcome = execute_review(repo, diff_spec="HEAD", mode="quick", paths="src", house_rules="")
    assert any("仅审查路径 src" in note for note in outcome.collection.notes)
    assert all(u.primary_file.startswith("src/") for u in outcome.units)


def test_web_pathspec_reaches_pipeline_and_bad_path_is_400(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEERRATUM_BASE_URL", "http://stub.invalid/v1")
    monkeypatch.setenv("CODEERRATUM_MODEL", "gpt-stub")
    monkeypatch.setenv("CODEERRATUM_MODE", "quick")
    monkeypatch.delenv("CODEERRATUM_MODELS", raising=False)
    monkeypatch.setattr(orchestrator, "_make_provider", lambda spec: StubProvider())
    repo = _init_repo(tmp_path / "repo")

    app = WebApp(repo)
    httpd = make_server(app, port=0)
    import threading

    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        with httpx.Client(
            base_url=base,
            timeout=30,
            trust_env=False,
            headers={"Origin": base},
        ) as client:
            bad = client.post("/api/review", json={"diff": "HEAD", "paths": "../etc"})
            assert bad.status_code == 400
            assert "相对路径" in bad.json()["error"]

            good = client.post("/api/review", json={"diff": "HEAD", "paths": "src"})
            assert good.status_code == 200
            run = client.get(f"/api/runs/{good.json()['run_id']}").json()
            assert any("仅审查路径 src" in note for note in run.get("notes", []))
    finally:
        httpd.shutdown()
        httpd.server_close()
