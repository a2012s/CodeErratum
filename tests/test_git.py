import subprocess
from pathlib import Path

from codeerratum.ingest import get_commit, split_diff


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
