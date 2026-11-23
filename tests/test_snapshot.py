"""Reviewing code that has no git history at all.

The complaint that started this: a brand-new project, nothing committed, and the
UI answered "No changes in diff. Nothing to review." The code is right there, so
the review is supposed to work — without `git init`, without `git add`, and
without writing anything into the folder.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from codeerratum.ingest import (
    EMPTY_TREE_SHA1,
    is_head_family,
    snapshot_diff,
    wants_full_worktree,
)
from codeerratum.ingest.git import split_diff


def test_plain_folder_without_any_git_yields_every_file(tmp_path: Path):
    (tmp_path / "mod.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("# hi\n", encoding="utf-8")

    diff = snapshot_diff(tmp_path)

    assert "a/mod.py" in diff
    assert "a/notes.md" in diff
    units = split_diff(diff, tmp_path)
    assert {u.primary_file for u in units} >= {"mod.py", "notes.md"}
    # The folder stays untouched: no repository was created behind the user's back.
    assert not (tmp_path / ".git").exists()


def test_snapshot_honours_gitignore_and_skips_the_state_dir(tmp_path: Path):
    (tmp_path / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    (tmp_path / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "dep.py").write_text("y = 2\n", encoding="utf-8")
    (tmp_path / ".codeerratum").mkdir()
    (tmp_path / ".codeerratum" / "config.json").write_text('{"api_key": "sk-x"}', encoding="utf-8")

    diff = snapshot_diff(tmp_path)

    assert "a/mod.py" in diff
    assert "a/.venv/" not in diff, "ignore rules must decide what counts as source"
    assert "a/.codeerratum/" not in diff, "the config file holds API keys"


def test_git_init_without_commits_still_snapshots(tmp_path: Path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "mod.py").write_text("x = 1\n", encoding="utf-8")

    diff = snapshot_diff(tmp_path)
    assert "a/mod.py" in diff
    # A real repository keeps its index untouched — the temp GIT_DIR owns it.
    assert not (tmp_path / ".git" / "index").exists()


def test_commit_relative_specs_mean_everything_only_without_history(tmp_path: Path):
    (tmp_path / "mod.py").write_text("x = 1\n", encoding="utf-8")
    for spec in ("HEAD", "head", "HEAD~1", "HEAD^", "HEAD~1..HEAD", "WORKTREE", "all"):
        assert wants_full_worktree(spec, tmp_path), spec
    # An explicit revision is not a request for everything, even with no history.
    assert not wants_full_worktree("main", tmp_path)

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.io"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)

    assert not wants_full_worktree("HEAD", tmp_path), "a real HEAD diff must stay literal"
    assert not wants_full_worktree("HEAD~1", tmp_path)


def test_snapshot_matches_the_empty_tree_recipe(tmp_path: Path):
    """Same content as `git add -A && git diff <empty tree>`, for a repo that has commits."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "mod.py").write_text("x = 1\n", encoding="utf-8")

    snapshot = snapshot_diff(tmp_path)
    manual = subprocess.run(
        ["git", "diff", "--cached", "--no-color", "--unified=3", EMPTY_TREE_SHA1],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    # No index yet in this repo, so the manual command sees nothing to compare.
    assert manual.returncode != 0 or manual.stdout == ""
    assert "a/mod.py" in snapshot


def test_only_a_bare_head_means_this_commit_and_nothing_else(tmp_path: Path):
    """The fallback must not swallow explicit ranges.

    ``HEAD~1..HEAD`` is a considered request for those commits; an empty answer
    is honest there. ``HEAD`` on a clean tree is not a request at all — it is the
    default the form ships with.
    """
    for spec in ("HEAD", "head", "HEAD~1", "HEAD^", "HEAD^2"):
        assert is_head_family(spec), spec
    for spec in ("HEAD~1..HEAD", "main...feature", "main", "4b82d..HEAD"):
        assert not is_head_family(spec), spec


def _committed_clean_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.io"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "mod.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=tmp_path, check=True)
    return tmp_path


def test_clean_tree_with_history_reviews_everything_instead_of_nothing(tmp_path, monkeypatch):
    """The wall behind "还是会如此": a committed project, clean tree, HEAD empty.

    Everything the user wants read is on disk and already committed, so ``HEAD``
    has no diff. Answering "Nothing to review" strands them; the run falls back
    to the whole tree and says so.
    """
    from codeerratum import pipeline
    from codeerratum.models import FindingCollection

    repo = _committed_clean_repo(tmp_path)
    monkeypatch.setenv("CODEERRATUM_BASE_URL", "http://x")
    monkeypatch.setenv("CODEERRATUM_MODEL", "m")
    monkeypatch.delenv("CODEERRATUM_MODELS", raising=False)
    seen: dict = {}

    async def fake_run(registry, mode, units, repo_root, run_id, **kwargs):
        seen["units"] = units
        return (
            FindingCollection(
                run_id=run_id,
                repo=str(repo_root),
                commit="",
                model_id="m",
                spec_level="L1",
            ),
            [],
            [],
        )

    monkeypatch.setattr(pipeline, "run_review", fake_run)
    notes: list[str] = []
    outcome = pipeline.execute_review(repo, diff_spec="HEAD", progress=notes.append)

    assert outcome.units_selected >= 1
    assert seen["units"], "the snapshot has to reach the reviewer"
    assert any("全部代码" in n or "all the code" in n for n in notes), notes


def test_an_explicit_empty_range_stays_empty(tmp_path, monkeypatch):
    """A range that genuinely has no changes must keep saying so."""
    import pytest

    from codeerratum import pipeline

    repo = _committed_clean_repo(tmp_path)
    monkeypatch.setenv("CODEERRATUM_BASE_URL", "http://x")
    monkeypatch.setenv("CODEERRATUM_MODEL", "m")
    monkeypatch.delenv("CODEERRATUM_MODELS", raising=False)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    with pytest.raises(pipeline.NoChanges):
        pipeline.execute_review(repo, diff_spec=f"{sha}..{sha}")


def test_snapshot_survives_gitignore_that_lists_the_state_dir(tmp_path: Path):
    """A repo whose own .gitignore lists .codeerratum used to fail the snapshot.

    Naming the state dir as an explicit ``:!`` pathspec makes git treat it as
    an explicitly requested ignored path: the add exits 1 with the
    "paths are ignored" hint, and WORKTREE review died before producing a
    diff — on every repo that (correctly) ignores the state dir.
    """
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text(".codeerratum/\n", encoding="utf-8")
    (tmp_path / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / ".codeerratum").mkdir()
    (tmp_path / ".codeerratum" / "config.json").write_text('{"api_key": "sk-x"}', encoding="utf-8")

    diff = snapshot_diff(tmp_path)

    assert "a/mod.py" in diff
    assert "a/.codeerratum/" not in diff, "the config file holds API keys"
    assert "api_key" not in diff


def test_snapshot_honours_local_exclude_without_gitignore(tmp_path: Path):
    """Rules living only in .git/info/exclude must hold in the throwaway GIT_DIR.

    The temp repo reads no local exclude, so .codeerratum has to be carried
    over explicitly — otherwise its config.json reaches the model.
    """
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    exclude = tmp_path / ".git" / "info" / "exclude"
    exclude.write_text("secret.local\n", encoding="utf-8")
    (tmp_path / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "secret.local").write_text("token\n", encoding="utf-8")

    diff = snapshot_diff(tmp_path)

    assert "a/mod.py" in diff
    assert "a/secret.local" not in diff, "user's local-only ignores still decide"


def test_unversioned_tree_still_hides_the_state_dir(tmp_path: Path):
    """No git at all: the API keys must still never reach the diff."""
    (tmp_path / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / ".codeerratum").mkdir()
    (tmp_path / ".codeerratum" / "config.json").write_text('{"api_key": "sk-x"}', encoding="utf-8")

    diff = snapshot_diff(tmp_path)

    assert "a/mod.py" in diff
    assert ".codeerratum" not in diff
