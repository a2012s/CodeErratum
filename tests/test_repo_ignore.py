"""The state directory holds API keys; git must never stage it.

`.codeerratum/config.json` is written into the reviewed repository with a 0600
mode, but git does not honour file modes — a `git add -A` there would commit the
keys. The rule goes to `.git/info/exclude` rather than `.gitignore` so the
reviewed repository's own diff stays untouched.
"""

from __future__ import annotations

import subprocess

from codeerratum import repo_ignore


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def test_exclude_rule_is_added_and_not_duplicated(tmp_path):
    git(tmp_path, "init", "-q")
    repo_ignore._seen.clear()

    repo_ignore.ensure_state_dir_ignored(tmp_path)
    repo_ignore._seen.clear()  # force a second real run
    repo_ignore.ensure_state_dir_ignored(tmp_path)

    exclude = tmp_path / ".git" / "info" / "exclude"
    text = exclude.read_text(encoding="utf-8")
    assert text.count(".codeerratum/") == 1

    # Prove git agrees, rather than trusting the file we just wrote.
    (tmp_path / ".codeerratum").mkdir()
    (tmp_path / ".codeerratum" / "config.json").write_text("{}", encoding="utf-8")
    check = git(tmp_path, "check-ignore", "-q", ".codeerratum/config.json")
    assert check.returncode == 0, check.stderr


def test_untracked_file_outside_the_rule_still_shows_up(tmp_path):
    """Guard against an over-broad pattern: only .codeerratum/ is excluded."""
    git(tmp_path, "init", "-q")
    repo_ignore._seen.clear()
    repo_ignore.ensure_state_dir_ignored(tmp_path)

    (tmp_path / "mod.py").write_text("x = 1\n", encoding="utf-8")
    status = git(tmp_path, "status", "--porcelain", "--untracked-files=all")
    assert "mod.py" in status.stdout


def test_non_repository_is_a_no_op(tmp_path):
    repo_ignore._seen.clear()
    repo_ignore.ensure_state_dir_ignored(tmp_path)  # must not raise
    assert not (tmp_path / ".git").exists()
