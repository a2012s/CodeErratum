import subprocess
from pathlib import Path

import pytest

from codeerratum.ingest import EMPTY_TREE_SHA1, get_commit, get_diff, split_diff
from codeerratum.ingest.git import DiffError, has_commits


def make_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.io"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "mod.py").write_text(
        "def alpha():\n    return 1\n\ndef beta(x):\n    return x + 1\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    return tmp_path


def diff_of(repo: Path, spec: str = "HEAD") -> str:
    return subprocess.run(
        ["git", "show", "--format=", "--unified=3", spec],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_empty_tree_reviews_a_codebase_with_no_history(tmp_path):
    """The recipe the UI points at when a folder has no .git of its own.

    A fresh ``git init`` has no HEAD, so ``git diff HEAD`` is unusable; the empty
    tree reports every staged file as an addition. Untracked files are invisible
    to any diff, which is why the message says to ``git add`` first.
    """
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "mod.py").write_text(
        "def alpha():\n    return 1\n\ndef beta(x):\n    return x + 1\n", encoding="utf-8"
    )
    assert get_diff(EMPTY_TREE_SHA1, tmp_path) == ""  # nothing staged: nothing to review

    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    units = split_diff(get_diff(EMPTY_TREE_SHA1, tmp_path), tmp_path)
    # A whole added file also yields a module-level unit (its imports/blank lines),
    # so compare as a subset rather than pinning the exact split.
    assert {"alpha", "beta"} <= {u.symbol for u in units}
    # No commit exists, and reporting that must stay non-fatal for the report header.
    assert get_commit(tmp_path) == "unknown"


def test_split_by_symbol(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "mod.py").write_text(
        "def alpha():\n    return 2\n\ndef beta(x):\n    return x + 2\n", encoding="utf-8"
    )
    diff = subprocess.run(
        ["git", "diff", "--unified=3"], cwd=repo, capture_output=True, text=True
    ).stdout
    units = split_diff(diff, repo)
    symbols = {u.symbol for u in units}
    assert "alpha" in symbols
    assert "beta" in symbols
    assert all(u.changed_lines > 0 for u in units)


def test_commit_retrieved(tmp_path):
    repo = make_repo(tmp_path)
    assert len(get_commit(repo)) == 40


def test_head_without_a_single_commit_names_the_empty_tree(tmp_path):
    """`git init` + `git add`, never committed: HEAD is an unborn branch.

    The old message said "this repository may have only one commit", which sends
    people looking for a commit that does not exist. The empty tree is the real
    way out, and it has to be in the text or the advice is a dead end.
    """
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "mod.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)

    assert has_commits(tmp_path) is False
    with pytest.raises(DiffError) as excinfo:
        get_diff("HEAD", tmp_path)
    message = str(excinfo.value)
    assert EMPTY_TREE_SHA1 in message
    assert "git commit" in message


def test_head_tilde_after_one_commit_keeps_the_old_advice(tmp_path):
    """With a commit present HEAD resolves, so only HEAD~1 is unresolvable."""
    repo = make_repo(tmp_path)
    assert has_commits(repo) is True
    with pytest.raises(DiffError) as excinfo:
        get_diff("HEAD~1", repo)
    assert "HEAD~1" in str(excinfo.value)
