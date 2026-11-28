"""Whitespace filtering is opt-in, graded, and safe for indentation-sensitive code.

A formatting-only diff should not buy a paid model review, but `-w` eats Python
indentation, which is syntax. These tests pin the graded modes and the three
entry points that expose them.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codeerratum import cli
from codeerratum.ingest.git import get_diff, split_diff
from codeerratum.mcp.server import TOOLS
from codeerratum.models.finding import FindingCollection
from codeerratum.pipeline import PipelineError
from codeerratum.web import WebApp


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "test")
    (tmp_path / "a.ts").write_text("const x = 1;\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")
    return tmp_path


def test_eol_mode_drops_trailing_whitespace_only_change(repo: Path) -> None:
    (repo / "a.ts").write_text("const x = 1;   \n", encoding="utf-8")
    assert get_diff("HEAD", repo).strip()  # the default still sees it

    assert get_diff("HEAD", repo, whitespace="eol") == ""
    assert split_diff(get_diff("HEAD", repo, whitespace="eol"), repo) == []


def test_all_mode_keeps_python_indentation_change(repo: Path) -> None:
    (repo / "b.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "python")

    (repo / "b.py").write_text("def f():\n        return 1\n", encoding="utf-8")
    for mode in ("all", "change", "eol"):
        diff = get_diff("HEAD", repo, whitespace=mode)
        assert "return 1" in diff, mode
        assert split_diff(diff, repo), mode


def test_all_mode_ignores_whitespace_in_plain_text(repo: Path) -> None:
    (repo / "a.ts").write_text("const x  =  1;\n", encoding="utf-8")
    assert get_diff("HEAD", repo).strip()
    assert get_diff("HEAD", repo, whitespace="all") == ""


@pytest.mark.parametrize("mode", ["bogus", "ALL", "-w"])
def test_invalid_mode_is_rejected(repo: Path, mode: str) -> None:
    with pytest.raises(ValueError, match="whitespace"):
        get_diff("HEAD", repo, whitespace=mode)


class _Outcome:
    def __init__(self, run_dir: Path):
        self.collection = FindingCollection(
            run_id="r1", repo=".", commit="c", model_id="m", spec_level="L1", findings=[]
        )
        self.run_dir = run_dir
        self.out_path = run_dir / "erratumReview.md"
        self.calls: list = []

    def summary(self) -> dict:
        return {
            "findings": 0,
            "dropped": 0,
            "failed_units": 0,
            "static_diags": 0,
            "calls": 0,
        }


def test_cli_passes_whitespace(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    seen: dict = {}

    def fake(*_args, **kwargs):
        seen.update(kwargs)
        return _Outcome(repo)

    monkeypatch.setattr(cli, "execute_review", fake)
    result = CliRunner().invoke(cli.app, ["review", "--repo", str(repo), "--whitespace", "eol"])
    assert result.exit_code == 0, result.output
    assert seen["whitespace"] == "eol"


def test_mcp_schema_exposes_whitespace() -> None:
    schema = next(t for t in TOOLS if t["name"] == "review_diff")["inputSchema"]
    prop = schema["properties"]["whitespace"]
    assert prop["enum"] == ["keep", "eol", "change", "all"]
    assert "whitespace" not in schema.get("required", [])


def test_web_prepare_review_validates_whitespace(repo: Path) -> None:
    app = WebApp(repo)
    assert app.prepare_review({"diff": "HEAD", "whitespace": "all"})["whitespace"] == "all"
    assert app.prepare_review({"diff": "HEAD"})["whitespace"] == "keep"
    with pytest.raises(PipelineError):
        app.prepare_review({"diff": "HEAD", "whitespace": "bogus"})
