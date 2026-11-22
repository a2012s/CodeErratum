"""Keep the repo-local state directory out of the user's commits.

``.codeerratum/`` is written next to the code being reviewed, and the
``config.json`` inside it holds API keys in clear text (0600 on disk, but git
does not read file modes). A plain ``git add -A`` in the reviewed repository
would stage the keys, and once committed they are in the history for good.

The reviewed repository's own ``.gitignore`` belongs to its owner — adding a
line there shows up in their diff. ``.git/info/exclude`` is the local-only
equivalent: it is not tracked, does not appear in ``git status``, and exists in
every repository, so the protection travels with no working-tree change at all.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

STATE_DIR = ".codeerratum"

# The check costs a subprocess; a review run asks once per repo, and the UI polls
# nothing that reaches here, but repeating it per invocation would be silly.
_seen: set[str] = set()


def ensure_state_dir_ignored(repo_root: Path, name: str = STATE_DIR) -> None:
    """Append ``name/`` to this clone's exclude file, once. Never raises."""
    try:
        key = str(Path(repo_root).resolve())
    except OSError:
        return
    if key in _seen:
        return
    _seen.add(key)

    probe = subprocess.run(
        ["git", "rev-parse", "--git-path", "info/exclude"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        return  # not a git repository: nothing can stage the folder
    exclude = Path(probe.stdout.strip() or "info/exclude")
    if not exclude.is_absolute():
        exclude = Path(repo_root) / exclude

    rule = f"{name.rstrip('/')}/"
    try:
        existing = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    except OSError:
        return
    if any(line.strip() == rule for line in existing.splitlines()):
        return
    try:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open("a", encoding="utf-8") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            fh.write(f"# CodeErratum state, incl. API keys — never commit\n{rule}\n")
    except OSError:
        return
