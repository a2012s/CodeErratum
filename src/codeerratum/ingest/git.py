"""Parse git diff into ReviewUnits split by modified symbol."""

from __future__ import annotations

import ast
import bisect
import os
import re
import subprocess
import tempfile
from pathlib import Path

from ..i18n import t
from ..models import Hunk, ReviewUnit
from ..repo_ignore import STATE_DIR as STATE_DIR_NAME

# The empty tree as a diff base: `git diff <this>` reports every tracked file as
# an addition. It is the only way to review a codebase that was copied in without
# history (a fresh `git init` has no HEAD to diff against). Staged files only — an
# untracked file never enters `git diff`, whatever the base is.
EMPTY_TREE_SHA1 = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

# A git invocation that outlives this is stuck (a hung fsck, a global hook, a
# dead network mount) — abort it instead of blocking the pipeline forever.
# snapshot_diff has its own, larger budget because it walks the whole tree.
GIT_TIMEOUT_S = 120

DIFF_HEADER = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+)$")

# Whitespace filtering is a graded opt-in, not a single `-w` switch. `-w` and
# even `-b` erase leading indentation, which is syntax in Python/YAML/Ruby;
# for those files the strongest mode degrades to end-of-line only.
WHITESPACE_MODES = ("keep", "eol", "change", "all")
_INDENT_SENSITIVE_SUFFIXES = frozenset(
    {
        ".py",
        ".yaml",
        ".yml",
        ".rb",
        ".coffee",
        ".hs",
        ".elm",
        ".purs",
        ".haml",
        ".slim",
        ".ex",
        ".exs",
    }
)
_INDENT_SENSITIVE_NAMES = frozenset({"makefile", "gemfile", "rakefile", "podfile"})


def normalize_whitespace(value: str | None) -> str:
    # Strict lowercase: the CLI enum, MCP schema and Web select all advertise
    # these exact spellings, so accepting "ALL" in one path only would drift.
    mode = (value or "keep").strip() or "keep"
    if mode not in WHITESPACE_MODES:
        raise ValueError(f"whitespace must be one of {', '.join(WHITESPACE_MODES)}")
    return mode


def _indent_sensitive(path: str) -> bool:
    name = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name in _INDENT_SENSITIVE_NAMES or Path(name).suffix in _INDENT_SENSITIVE_SUFFIXES


def _whitespace_flags(mode: str, diff_text: str) -> list[str]:
    if mode == "keep":
        return []
    if mode == "eol":
        return ["--ignore-space-at-eol"]
    changed = [m.group("b") for line in diff_text.splitlines() if (m := DIFF_HEADER.match(line))]
    if any(_indent_sensitive(path) for path in changed):
        # `-b` also collapses leading whitespace, so it is not safe here.
        return ["--ignore-space-at-eol"]
    return ["--ignore-space-change"] if mode == "change" else ["--ignore-all-space"]


HUNK_HEADER = re.compile(
    r"^@@ -(?P<old>\d+)(?:,(?P<old_n>\d+))? \+(?P<new>\d+)(?:,(?P<new_n>\d+))? @@"
)


class DiffError(RuntimeError):
    """Raised when git cannot produce the requested diff."""


def _run_git(
    cmd: list[str], repo_root: Path, *, binary: bool = False, timeout: int = GIT_TIMEOUT_S
) -> subprocess.CompletedProcess | None:
    """Run git with a hard timeout; ``None`` means it hung and was killed."""
    try:
        return subprocess.run(
            cmd,
            cwd=repo_root,
            capture_output=True,
            text=not binary,
            errors=None if binary else "replace",
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None
    except OSError:
        return None


def normalize_pathspec(paths: str) -> str:
    """Validate whitespace-separated git pathspecs from UI/CLI input.

    Values are passed to git as separate arguments after ``--`` (so no shell and
    no option injection), but a user can still type an absolute path or ``..``
    and accidentally review files outside the intended tree. Reject those early.
    """
    parts = paths.split()
    for value in parts:
        # Treat backslashes as separators too: Path on POSIX does not, so
        # "..\\secret" would otherwise slip through as a literal filename.
        normalized = value.replace("\\", "/")
        if normalized.startswith("/") or ".." in Path(normalized).parts:
            raise DiffError(t("git.pathspec_relative"))
    return paths.strip()


def get_diff(spec: str, repo_root: Path, paths: str = "", whitespace: str = "keep") -> str:
    """Resolve a diff spec through git. Supports ranges and optional pathspecs.

    ``paths`` is split on whitespace and appended after ``--`` as separate git
    arguments, so each token is treated as a path, never as a flag.

    ``spec`` is a revision, not an option: ``git diff --output=/tmp/x HEAD~1``
    would write the diff to an arbitrary file and return an empty stdout, which
    looks exactly like "no changes". Reject option-looking specs up front (clear
    message) and pass ``--end-of-options`` so git itself refuses anything that
    still tries to parse as a flag.
    """
    spec = spec.strip()
    if not spec:
        raise DiffError("diff spec must not be empty")
    if spec.startswith("-"):
        raise DiffError("diff spec must not start with '-': git would parse it as an option")
    mode = normalize_whitespace(whitespace)
    pathspec = normalize_pathspec(paths)
    cmd = ["git", "diff", "--no-color", "--unified=3", "--end-of-options", spec]
    if pathspec:
        cmd += ["--", *pathspec.split()]

    def checked(parts: list[str]) -> str:
        result = _run_git(parts, repo_root)
        if result is None:
            raise DiffError(t("git.timeout", seconds=GIT_TIMEOUT_S, cmd=" ".join(parts)))
        if result.returncode != 0:
            detail = result.stderr.strip() or f"git diff {spec} failed"
            if "ambiguous argument" in detail:
                if not has_commits(repo_root):
                    # A copied-in codebase that was `git init`ed but never committed:
                    # HEAD does not exist at all, so both the fix and the vocabulary
                    # of "one commit" are wrong. Name the empty-tree recipe instead.
                    detail = t("git.no_commits", tree=EMPTY_TREE_SHA1, spec=spec, detail=detail)
                elif spec.startswith("HEAD~"):
                    # A repo with one commit has no HEAD~1. The raw git message is
                    # English and suggests an unrelated fix (--), which leaves users
                    # stuck; say what to type instead and keep the original for copying.
                    detail = t("git.diff_unresolvable", spec=spec, detail=detail)
            raise DiffError(detail)
        return result.stdout

    raw = checked(cmd)
    if mode == "keep":
        return raw
    flags = _whitespace_flags(mode, raw)
    if not flags:
        return raw
    # Re-run with the ignore flags, but only after inspecting the real diff:
    # `all` has to step down for indentation-sensitive files.
    at = cmd.index("--end-of-options")
    return checked([*cmd[:at], *flags, *cmd[at:]])


def has_commits(repo_root: Path) -> bool:
    """Whether HEAD resolves to an actual commit.

    ``git init`` writes a HEAD that points at an unborn branch: the ref exists,
    ``git rev-parse HEAD`` still fails. The distinction decides which remedy to
    print, so it is checked rather than guessed from the spec.
    """
    result = _run_git(["git", "rev-parse", "--verify", "--quiet", "HEAD"], repo_root)
    return result is not None and result.returncode == 0


def is_git_repo(repo_root: Path) -> bool:
    """True when ``git`` can find a repository for this directory.

    Asks git rather than testing ``.git``: a subdirectory of a repository has no
    ``.git`` of its own but is still inside one.
    """
    result = _run_git(["git", "rev-parse", "--git-dir"], repo_root)
    return result is not None and result.returncode == 0


def wants_full_worktree(spec: str, repo_root: Path) -> bool:
    """Whether a commit-relative spec can only mean "review everything".

    A repository with zero commits has no HEAD, no HEAD~1 and no HEAD^, so a
    spec spelled from those cannot describe anything else. Neither does a plain
    folder with no repository at all. Refusing outright ("ambiguous argument",
    "not a git repository") is technically right and practically useless: the
    code is sitting there and the user asked to review it. Also accepts the
    explicit aliases WORKTREE / ALL, which work with or without history.
    """
    upper = spec.strip().upper()
    if upper in ("WORKTREE", "ALL"):
        return True
    head_ref = upper.split("...")[0].split("..")[0]
    if not (head_ref == "HEAD" or head_ref.startswith(("HEAD~", "HEAD^"))):
        return False
    if not is_git_repo(repo_root):
        return True
    return not has_commits(repo_root)


def is_head_family(spec: str) -> bool:
    """A spec that names one commit-relative point, never an explicit range.

    ``HEAD~1..HEAD`` and ``main...feature`` say "these commits and no others",
    so an empty diff there is a real answer. Bare ``HEAD`` / ``HEAD~1`` do not:
    when they resolve to nothing the only reason is that everything is already
    committed, and the code the user wants read is still in the working tree.
    """
    s = spec.strip()
    if ".." in s:
        return False
    upper = s.upper()
    return upper == "HEAD" or upper.startswith(("HEAD~", "HEAD^"))


# A first review of an unversioned tree walks the whole folder; anything past
# this is almost certainly a missing .gitignore (.venv, node_modules, build).
SNAPSHOT_TIMEOUT_S = 180


def snapshot_diff(repo_root: Path, paths: str = "", whitespace: str = "keep") -> str:
    """Every file that would be committed, as an addition diff.

    Built inside a throwaway git directory (``GIT_DIR`` in a temp dir, work tree
    pointing at the real folder), so the user's repository is untouched: no
    ``git init``, no index write, no blobs in their object database. Their
    ``.gitignore`` still decides what counts as source, which is the whole point
    — dumping ``.venv`` and ``node_modules`` into a review would be worse than
    refusing.

    ``.codeerratum/`` is excluded via ``core.excludesFile`` on top of the
    tree's own ``.gitignore``: it holds ``config.json`` with API keys, and a
    repo whose rule lives only in ``.git/info/exclude`` would otherwise have
    it staged (the throwaway GIT_DIR reads no local exclude) and echoed back
    through a model. Naming it as an explicit ``:!`` pathspec does not work:
    git then treats it as an explicitly requested ignored path and *fails*
    the add with exit 1 whenever the tree's ``.gitignore`` also lists it.
    """
    pathspec = normalize_pathspec(paths)
    with tempfile.TemporaryDirectory(prefix="codeerratum-snapshot-") as tmp:
        # Merge the state-dir rule with the user's local exclude file into one
        # excludesFile for the throwaway repo. .gitignore (in-tree) keeps
        # applying on its own; core.excludesFile is additive, not a replacement.
        merged_excludes = Path(tmp) / "excludes"
        merged_excludes.write_text(
            f"# CodeErratum state, incl. API keys — never review or commit\n"
            f"{STATE_DIR_NAME.rstrip('/')}/\n",
            encoding="utf-8",
        )
        local_exclude = repo_root / ".git" / "info" / "exclude"
        if local_exclude.is_file():
            try:
                merged_excludes.write_text(
                    merged_excludes.read_text(encoding="utf-8")
                    + "\n"
                    + local_exclude.read_text(encoding="utf-8", errors="replace"),
                    encoding="utf-8",
                )
            except OSError:
                pass  # local rules are best-effort; the state-dir rule is already in
        env = {
            **os.environ,
            "GIT_DIR": str(Path(tmp) / "git"),
            "GIT_WORK_TREE": str(repo_root),
            # A leaked GIT_INDEX_FILE would point the throwaway repo at the
            # user's real index and stage their files for real.
            "GIT_INDEX_FILE": str(Path(tmp) / "index"),
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.excludesFile",
            "GIT_CONFIG_VALUE_0": str(merged_excludes),
        }

        def git(*args: str) -> subprocess.CompletedProcess[str]:
            try:
                return subprocess.run(
                    ["git", *args],
                    cwd=repo_root,
                    env=env,
                    capture_output=True,
                    text=True,
                    errors="replace",
                    check=False,
                    timeout=SNAPSHOT_TIMEOUT_S,
                )
            except subprocess.TimeoutExpired as exc:
                raise DiffError(t("git.snapshot_timeout", seconds=SNAPSHOT_TIMEOUT_S)) from exc

        init = git("init", "-q", "--template=")
        if init.returncode != 0:
            raise DiffError(init.stderr.strip() or "git init failed for the snapshot")

        targets = pathspec.split() if pathspec else ["."]
        added = git("add", "-A", "--", *targets)
        if added.returncode != 0:
            raise DiffError(added.stderr.strip() or "git add failed for the snapshot")

        args = ["diff", "--cached", "--no-color", "--unified=3", EMPTY_TREE_SHA1]
        first = git(*args)
        if first.returncode != 0:
            raise DiffError(first.stderr.strip() or "git diff failed for the snapshot")
        mode = normalize_whitespace(whitespace)
        if mode == "keep":
            return first.stdout
        flags = _whitespace_flags(mode, first.stdout)
        if not flags:
            return first.stdout
        second = git(*args[:4], *flags, *args[4:])
        if second.returncode != 0:
            raise DiffError(second.stderr.strip() or "git diff failed for the snapshot")
        return second.stdout


def get_commit(repo_root: Path) -> str:
    result = _run_git(["git", "rev-parse", "HEAD"], repo_root)
    return result.stdout.strip() if result is not None and result.returncode == 0 else "unknown"


def new_side_sources(spec: str, repo_root: Path, paths: str = "") -> dict[str, str]:
    """File contents of the diff's new side, read from the object database.

    ``git diff A..B`` describes B's tree. Reviewing it while A is checked out used
    to slice B's line numbers against A's text: the model saw the wrong region and
    the anchor hash still passed, because it validated the same wrong text it had
    shown the model. Reading B's blob removes that failure mode.

    Files whose new side is the working tree (all-zero object id) are omitted, so
    the caller falls back to the file on disk — that is the correct new side for
    ``git diff HEAD``. Deleted files are omitted as well: there is nothing to read
    and nothing to anchor.
    """
    spec = spec.strip() or "HEAD"
    if spec.startswith("-"):
        raise DiffError("diff spec must not start with '-': git would parse it as an option")
    pathspec = normalize_pathspec(paths)
    # --abbrev=40 keeps the whole object id in diff-raw (--full-index only
    # widens the "index abc..def" line in patch output). An abbreviated sha can
    # be a prefix of another blob's id and bind a file to the wrong snapshot.
    cmd = [
        "git",
        "diff",
        "--raw",
        "-z",
        "--no-color",
        "--abbrev=40",
        "--end-of-options",
        spec,
    ]
    if pathspec:
        cmd += ["--", *pathspec.split()]
    result = _run_git(cmd, repo_root, binary=True)
    if result is None:
        raise DiffError(t("git.timeout", seconds=GIT_TIMEOUT_S, cmd=" ".join(cmd)))
    if result.returncode != 0:
        stderr = (result.stderr or b"").decode(errors="replace").strip()
        raise DiffError(stderr or "git diff failed")

    wanted: list[tuple[str, str]] = []  # (path, new_sha)
    tokens = result.stdout.split(b"\0")
    index = 0
    while index < len(tokens):
        header = tokens[index]
        index += 1
        if not header.startswith(b":"):
            continue
        fields = header.split()
        if len(fields) < 5:
            continue
        new_sha = fields[3].decode()
        status = fields[4].decode()
        path = tokens[index].decode(errors="replace") if index < len(tokens) else ""
        index += 1
        if status[:1] in ("R", "C") and index < len(tokens):
            path = tokens[index].decode(errors="replace")  # destination of the rename/copy
            index += 1
        if status[:1] == "D" or not new_sha.strip("0"):
            continue
        wanted.append((path, new_sha))
    return _read_blobs(wanted, repo_root)


def _read_blobs(wanted: list[tuple[str, str]], repo_root: Path) -> dict[str, str]:
    """Read many blobs in one ``git cat-file --batch`` invocation.

    One subprocess per file meant hundreds of process spawns on a large diff —
    and, before the timeout landed, hundreds of places to hang. The batch
    protocol streams every blob in a single process; a missing sha comes back
    as a ``missing`` header instead of a nonzero exit.
    """
    sources: dict[str, str] = {}
    if not wanted:
        return sources
    stdin = ("\n".join(sha for _path, sha in wanted) + "\n").encode()
    try:
        proc = subprocess.run(
            ["git", "cat-file", "--batch"],
            cwd=repo_root,
            input=stdin,
            capture_output=True,
            check=False,
            timeout=GIT_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError):
        return sources  # degraded context: split_diff falls back to the worktree
    if proc.returncode != 0:
        return sources
    stream = proc.stdout
    blobs: list[tuple[str, bytes]] = []
    pos = 0
    while pos < len(stream):
        nl = stream.find(b"\n", pos)
        if nl < 0:
            break
        header = stream[pos:nl].split()
        if len(header) < 2 or header[1] != b"blob":
            # "<sha> missing" or a malformed line: skip just this record.
            pos = nl + 1
            continue
        size = int(header[2])
        start = nl + 1
        blobs.append((header[0].decode(), stream[start : start + size]))
        pos = start + size + 1  # trailing newline after the payload
    for path, sha in wanted:
        # --full-index above makes both sides 40-char ids, so equality is safe;
        # prefix matching could bind a file to a different blob whose id merely
        # starts with the same abbreviated sha.
        data = next((payload for full_sha, payload in blobs if full_sha == sha), None)
        if data is not None:
            sources[path] = data.decode("utf-8", errors="replace")
    return sources


def _iter_files(diff_text: str):
    """Yield (path, hunks, is_deleted) per file from a unified diff.

    The trailing hunk body must be attached before crossing a file boundary,
    otherwise every file except the last one loses its final hunk content.
    """
    current: str | None = None
    hunks: list[Hunk] = []
    body: list[str] = []
    deleted = False

    def flush_body() -> None:
        if body and hunks:
            hunks[-1].content = "\n".join(body)
            body.clear()

    def flush_file():
        flush_body()
        if current is not None and hunks:
            return current, hunks, deleted
        return None

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            done = flush_file()
            if done:
                yield done
            m = DIFF_HEADER.match(line)
            current = m.group("b") if m else None
            hunks = []
            body = []
            deleted = False
            continue
        if line.startswith("deleted file mode"):
            deleted = True
            continue
        if line.startswith("+++ ") and not hunks:
            if line[4:].strip() == "/dev/null":
                deleted = True
            continue
        if current is None:
            continue
        m = HUNK_HEADER.match(line)
        if m:
            flush_body()
            hunks.append(
                Hunk(
                    old_start=int(m.group("old")),
                    old_lines=int(m.group("old_n") or 1),
                    new_start=int(m.group("new")),
                    new_lines=int(m.group("new_n") or 1),
                    content="",
                )
            )
            continue
        if hunks:
            body.append(line)
    done = flush_file()
    if done:
        yield done


def _build_symbol_index(source: str) -> list[tuple[int, int, str, str]]:
    """Return (start_line, end_line, qualified_name, kind) for top-level defs."""
    index: list[tuple[int, int, str, str]] = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return index
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            kind = "class" if isinstance(node, ast.ClassDef) else "function"
            name = node.name
            if isinstance(node, ast.ClassDef):
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        index.append(
                            (
                                child.lineno,
                                child.end_lineno or child.lineno,
                                f"{name}.{child.name}",
                                "function",
                            )
                        )
            index.append((node.lineno, node.end_lineno or node.lineno, name, kind))
    index.sort(key=lambda x: x[0])
    return index


def _symbol_for_line(index: list[tuple[int, int, str, str]], line: int) -> tuple[str, str]:
    """Smallest symbol span containing ``line`` (index is sorted by start line).

    Top-level declarations nest (a method inside its class), so the nearest
    preceding start whose span still contains the line is the smallest
    containing span — which is why a backwards scan from the bisect position
    stops at the first hit instead of walking the whole index.
    """
    pos = bisect.bisect_right(index, line, key=lambda item: item[0])
    for start, end, name, kind in reversed(index[:pos]):
        if start <= line <= end:
            return name, kind
    return "<module>", "module"


# A single unit with 900 changed lines turns one model call into a 4–5 minute
# tail and hides defects between unrelated functions. Above this threshold we
# re-split by symbol (or by line window when no symbol boundary exists). The
# excerpt cap in the executor keeps the *context* bounded, but it still gets one
# prompt per oversized unit — splitting gives each region its own call.
MAX_UNIT_CHANGED_LINES = 220

# Top-level declaration detector for languages without an AST index here. Only
# used for oversized units, so a false positive cannot change small-file reviews.
_GENERIC_DECL = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:async\s+)?"
    r"(?:(?:public|private|protected|static|final|abstract|override|virtual|pub)\s+)*"
    r"(function|class|interface|struct|enum|trait|impl|mod|namespace|type|func|def|fn)"
    r"\s+([A-Za-z_$][\w$]*)"
)


def _build_generic_symbol_index(source: str) -> list[tuple[int, int, str, str]]:
    """Best-effort (start, end, name, kind) index for non-Python sources.

    A regex is deliberately not a parser: this only groups changed lines into
    review units, it never claims to understand the language. Each declaration
    runs until the next declaration (or EOF), which is enough to keep a 900-line
    diff from being reviewed as one prompt. Small files never reach this path.
    """
    lines = source.splitlines()
    hits: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(lines, 1):
        m = _GENERIC_DECL.match(line)
        if not m:
            continue
        keyword, name = m.group(1), m.group(2)
        class_words = ("class", "struct", "interface", "enum", "trait", "impl")
        kind = "class" if keyword in class_words else "function"
        hits.append((lineno, name, kind))
    index: list[tuple[int, int, str, str]] = []
    for i, (start, name, kind) in enumerate(hits):
        end = hits[i + 1][0] - 1 if i + 1 < len(hits) else len(lines)
        if end >= start:
            index.append((start, end, name, kind))
    return index


def _split_group_by_lines(
    file: str, symbol: str, kind: str, hunks: list[Hunk], changed: list[int], lang: str
) -> list[ReviewUnit]:
    """Last-resort split when one symbol still exceeds the line budget."""
    parts = [
        changed[i : i + MAX_UNIT_CHANGED_LINES]
        for i in range(0, len(changed), MAX_UNIT_CHANGED_LINES)
    ]
    if len(parts) < 2:
        return [
            ReviewUnit(
                unit_id=ReviewUnit.make_id(file, symbol, hunks),
                primary_file=file,
                symbol=symbol,
                kind=kind,  # type: ignore[arg-type]
                language=lang,
                hunks=hunks,
                changed_lines=len(changed),
                is_public=not symbol.startswith("_") and symbol != "<module>",
            )
        ]
    out: list[ReviewUnit] = []
    for part in parts:
        part_symbol = f"{symbol}:L{part[0]}-{part[-1]}"
        out.append(
            ReviewUnit(
                unit_id=ReviewUnit.make_id(file, part_symbol, hunks),
                primary_file=file,
                symbol=part_symbol,
                kind=kind,  # type: ignore[arg-type]
                language=lang,
                hunks=hunks,
                changed_lines=len(part),
                is_public=not symbol.startswith("_") and symbol != "<module>",
            )
        )
    return out


def _changed_lines(hunk: Hunk):
    """Yield (new_line_no, text) for every added/modified line in a hunk.

    A single hunk can span multiple symbols, so we must map each changed line
    individually instead of assigning the whole hunk by its first line.
    """
    new = hunk.new_start
    for line in hunk.content.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            yield new, line[1:]
            new += 1
        elif line.startswith("-") and not line.startswith("---"):
            continue
        elif line.startswith("\\"):
            continue
        else:
            new += 1


_TAG_WORD = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])")
_RISK_VOCAB: tuple[tuple[frozenset[str], str], ...] = (
    (
        frozenset(
            {
                "thread",
                "threading",
                "asyncio",
                "async",
                "await",
                "lock",
                "locks",
                "mutex",
                "semaphore",
                "concurrent",
                "concurrency",
                "race",
            }
        ),
        "concurrency",
    ),
    (
        frozenset(
            {
                "auth",
                "authentication",
                "authorize",
                "authorization",
                "oauth",
                "token",
                "tokens",
                "password",
                "passwords",
                "credential",
                "credentials",
                "jwt",
                "login",
                "permission",
                "permissions",
            }
        ),
        "auth",
    ),
    (frozenset({"migration", "migrations"}), "migration"),
    (frozenset({"transaction", "transactions"}), "data"),
)


def _risk_tags(content: str) -> list[str]:
    """Tag the changed code by identifier words, not substrings.

    Substring matching made ``author_name`` look like auth and ``blocking_call``
    look like a lock, and those tags steer the unit budget (``prioritize``). Words
    are split on non-letters and camelCase, so ``author`` no longer contains
    ``auth`` and ``blocking`` no longer contains ``lock``.
    """
    words = {match.group(0).lower() for match in _TAG_WORD.finditer(content)}
    return sorted(tag for vocab, tag in _RISK_VOCAB if words & vocab)


class _SymbolGroup:
    """Hunks plus the true count of changed lines, accumulated per symbol."""

    __slots__ = ("hunks", "changed")

    def __init__(self) -> None:
        self.hunks: list[Hunk] = []
        self.changed = 0

    def add_hunk(self, hunk: Hunk) -> None:
        if all(h is not hunk for h in self.hunks):
            self.hunks.append(hunk)


LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "jsx",
    ".mjs": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".rb": "ruby",
    ".php": "php",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".swift": "swift",
    ".sh": "bash",
    ".sql": "sql",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".json": "json",
    ".ini": "ini",
    ".md": "markdown",
    ".css": "css",
    ".html": "html",
}


def detect_language(file: str) -> str:
    """Map a path to a language tag. Unknown extensions become 'text'.

    Only Python gets symbol-level splitting; everything else is reviewed per file.
    That is a real limitation, not a silent claim of support: the tag reaches the
    prompt so the model knows what it is reading, and `language` is recorded on the
    unit so the report can show it.
    """
    return LANGUAGE_BY_SUFFIX.get(Path(file).suffix.lower(), "text")


def _hunk_matches_source(hunk: Hunk, source_lines: list[str]) -> bool:
    """Do this hunk's new-side lines exist at their claimed positions in the source?

    When the source is the diff revision's blob this is a parser self-check. When
    it is the working tree while the diff describes another revision, a mismatch
    means the model would be shown code at the wrong offsets — and the anchor hash
    would still pass, because it validates whatever text the model was given.
    Skipping the file, with a notice, is the honest degradation.
    """
    lineno = hunk.new_start
    for line in hunk.content.splitlines():
        if not line:
            continue
        prefix = line[0]
        if prefix == "-" or prefix == "\\":
            continue
        if prefix not in (" ", "+"):
            return False  # malformed hunk body: line cannot be placed on the new side
        if lineno > len(source_lines):
            return False
        expected = source_lines[lineno - 1]
        actual = line[1:].rstrip("\r")
        if expected != actual:
            return False
        lineno += 1
    return True


def split_diff(
    diff_text: str,
    repo_root: Path,
    language: str | None = None,
    notices: list[str] | None = None,
    sources: dict[str, str] | None = None,
) -> list[ReviewUnit]:
    """Split a unified diff into per-symbol ReviewUnits.

    ``sources`` supplies the new side's file contents read from the diff revision
    (see ``new_side_sources``). Without it the working tree is read; files whose
    contents disagree with their hunks are then skipped, because slicing line
    numbers from one revision against another's text reviews the wrong region
    while every anchor still checks out. Every skip is appended to ``notices`` so
    the degradation stays visible.

    ``language`` forces a single language for every file; by default it is inferred
    per file from the extension.
    """
    units: list[ReviewUnit] = []
    blob_sources = sources or {}
    for file, hunks, is_deleted in _iter_files(diff_text):
        if file == "/dev/null" or file.endswith((".lock", ".sum")):
            continue
        blob = blob_sources.get(file)
        source_path = repo_root / file
        if is_deleted or (blob is None and not source_path.exists()):
            if notices is not None:
                reason = "deleted" if is_deleted else "not present in working tree"
                notices.append(f"skipped {file} ({reason})")
            continue
        lang = language or detect_language(file)
        source = (
            blob if blob is not None else source_path.read_text(encoding="utf-8", errors="replace")
        )
        source_lines = source.splitlines()
        if hunks and not all(_hunk_matches_source(h, source_lines) for h in hunks):
            if notices is not None:
                notices.append(
                    f"skipped {file} (file contents do not match the diff revision; "
                    "check out the target revision and re-run)"
                )
            continue
        index = _build_symbol_index(source) if lang == "python" and source else []

        by_symbol: dict[tuple[str, str], _SymbolGroup] = {}
        group_lines: dict[tuple[str, str], list[int]] = {}
        for hunk in hunks:
            changed_lines = list(_changed_lines(hunk))
            if not changed_lines:
                symbol, kind = _symbol_for_line(index, hunk.new_start)
                by_symbol.setdefault((symbol, kind), _SymbolGroup()).add_hunk(hunk)
                group_lines.setdefault((symbol, kind), [])
                continue
            for new_line, _text in changed_lines:
                symbol, kind = _symbol_for_line(index, new_line)
                key = (symbol, kind)
                group = by_symbol.setdefault(key, _SymbolGroup())
                group.add_hunk(hunk)
                group.changed += 1
                group_lines.setdefault(key, []).append(new_line)

        generic_index: list[tuple[int, int, str, str]] | None = None
        for (symbol, kind), group in by_symbol.items():
            content = "\n".join(h.content for h in group.hunks)
            is_config = kind == "module" and file.endswith(
                (".yaml", ".yml", ".toml", ".json", ".ini")
            )
            if is_config:
                kind = "config"
                symbol = "<config>"

            if group.changed > MAX_UNIT_CHANGED_LINES and not is_config:
                # Python already has an AST index; other languages get a
                # best-effort one, built only for this oversized group.
                if lang != "python" and generic_index is None and source:
                    generic_index = _build_generic_symbol_index(source)
                use_index = index if lang == "python" else (generic_index or [])
                lines = group_lines.get((symbol, kind), [])
                if lines and use_index:
                    sub: dict[tuple[str, str], list[int]] = {}
                    for line in lines:
                        sub_symbol, sub_kind = _symbol_for_line(use_index, line)
                        sub.setdefault((sub_symbol, sub_kind), []).append(line)
                    if len(sub) > 1:
                        for (sub_symbol, sub_kind), sub_lines in sub.items():
                            if len(sub_lines) > MAX_UNIT_CHANGED_LINES:
                                units.extend(
                                    _split_group_by_lines(
                                        file, sub_symbol, sub_kind, group.hunks, sub_lines, lang
                                    )
                                )
                            else:
                                units.append(
                                    ReviewUnit(
                                        unit_id=ReviewUnit.make_id(file, sub_symbol, group.hunks),
                                        primary_file=file,
                                        symbol=sub_symbol,
                                        kind=sub_kind,  # type: ignore[arg-type]
                                        language=lang,
                                        hunks=group.hunks,
                                        changed_lines=len(sub_lines),
                                        is_public=not sub_symbol.startswith("_")
                                        and sub_symbol != "<module>",
                                        risk_tags=_risk_tags(content),
                                    )
                                )
                        continue
                # No usable symbol boundary: split the window instead.
                units.extend(
                    _split_group_by_lines(
                        file, symbol, kind, group.hunks, group_lines.get((symbol, kind), []), lang
                    )
                )
                continue

            units.append(
                ReviewUnit(
                    unit_id=ReviewUnit.make_id(file, symbol, group.hunks),
                    primary_file=file,
                    symbol=symbol,
                    kind=kind,  # type: ignore[arg-type]
                    language=lang,
                    hunks=group.hunks,
                    changed_lines=group.changed,
                    is_public=not symbol.startswith("_") and symbol != "<module>",
                    risk_tags=_risk_tags(content),
                )
            )
    return units
