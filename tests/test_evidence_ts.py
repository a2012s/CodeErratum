"""Tests for TypeScript/JavaScript static evidence support.

These tests use fake shell scripts (``tmp_path``) so they never need Node.js,
npm, tsc, or eslint to be installed on the machine.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from codeerratum.evidence.static import (
    _CACHE,
    StaticDiag,
    _cache_prune,
    collect_static,
    known_facts_block,
    reset_tsc_cache,
    run_eslint,
    run_tsc,
)


@pytest.fixture(autouse=True)
def _clear_caches():
    """These module-level memos are keyed by repo path; keep tests independent."""
    _CACHE.clear()
    reset_tsc_cache()
    yield
    _CACHE.clear()
    reset_tsc_cache()


def _make_fake_tool(dir: Path, name: str, content: str) -> Path:
    """Create a fake executable script inside *dir* and return its path."""
    tool = dir / name
    tool.write_text(content)
    tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
    return tool


def _ts_repo(tmp_path: Path) -> Path:
    """Minimal TS/JS repo with package.json, src/, and a stub source file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package.json").write_text("{}")
    (repo / "src").mkdir()
    (repo / "src" / "a.ts").write_text("const x: number = 1;\n")
    return repo


# ── tsc ──────────────────────────────────────────────────────────────────────


def test_run_tsc_not_found(tmp_path):
    """No tsc anywhere → empty list, no crash."""
    repo = _ts_repo(tmp_path)
    assert run_tsc("src/a.ts", repo) == []


def test_run_tsc_parses_error(tmp_path):
    """Fake tsc outputs a typical error → one StaticDiag parsed."""
    repo = _ts_repo(tmp_path)
    bin_dir = repo / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    _make_fake_tool(
        bin_dir,
        "tsc",
        (
            "#!/bin/sh\n"
            'echo "src/a.ts(12,5): error TS2345: Argument of type string is not assignable"\n'
        ),
    )
    diags = run_tsc("src/a.ts", repo)
    assert len(diags) == 1
    d = diags[0]
    assert d.tool == "tsc"
    assert d.file == "src/a.ts"
    assert d.line == 12
    assert d.code == "TS2345"
    assert "not assignable" in d.message
    assert "TS2345" not in d.message  # code lives in .code, not the message text


def test_run_tsc_filters_other_files(tmp_path):
    """tsc reports multiple files → only the asked-for file is kept."""
    repo = _ts_repo(tmp_path)
    bin_dir = repo / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    _make_fake_tool(
        bin_dir,
        "tsc",
        (
            "#!/bin/sh\n"
            'echo "src/other.ts(1,1): error TS1234: bad"\n'
            'echo "src/a.ts(2,3): error TS5678: also bad"\n'
        ),
    )
    diags = run_tsc("src/a.ts", repo)
    assert len(diags) == 1
    assert diags[0].file == "src/a.ts"


def test_run_tsc_warning_parsed(tmp_path):
    """tsc warnings are parsed alongside errors."""
    repo = _ts_repo(tmp_path)
    bin_dir = repo / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    _make_fake_tool(
        bin_dir,
        "tsc",
        '#!/bin/sh\necho "src/a.ts(5,1): warning TS6133: x is declared but never read"\n',
    )
    diags = run_tsc("src/a.ts", repo)
    assert len(diags) == 1
    assert diags[0].code == "TS6133"


def test_run_tsc_prefers_local_over_path(tmp_path, monkeypatch):
    """Repo-local tsc wins over PATH."""
    repo = _ts_repo(tmp_path)
    bin_dir = repo / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    _make_fake_tool(
        bin_dir,
        "tsc",
        '#!/bin/sh\necho "src/a.ts(1,1): error TS9999: from-local"\n',
    )
    # Also put a fake on PATH that would say something else.
    path_dir = tmp_path / "path-bin"
    path_dir.mkdir()
    _make_fake_tool(path_dir, "tsc", '#!/bin/sh\necho "src/a.ts(1,1): error TS8888: from-path"\n')
    monkeypatch.setenv("PATH", str(path_dir), prepend=":")
    diags = run_tsc("src/a.ts", repo)
    assert len(diags) == 1
    assert diags[0].code == "TS9999"


# ── eslint ───────────────────────────────────────────────────────────────────


def test_run_eslint_not_found(tmp_path):
    """No eslint → empty list."""
    repo = _ts_repo(tmp_path)
    assert run_eslint("src/a.ts", repo) == []


def test_run_eslint_parses_json(tmp_path):
    """Fake eslint outputs valid JSON → parsed as StaticDiag."""
    repo = _ts_repo(tmp_path)
    bin_dir = repo / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    payload = json.dumps(
        [
            {
                "filePath": "src/a.ts",
                "messages": [
                    {
                        "ruleId": "no-unused-vars",
                        "severity": 2,
                        "line": 3,
                        "message": "x is assigned but never used",
                    }
                ],
            }
        ]
    )
    _make_fake_tool(bin_dir, "eslint", f"#!/bin/sh\necho '{payload}'\n")
    diags = run_eslint("src/a.ts", repo)
    assert len(diags) == 1
    d = diags[0]
    assert d.tool == "eslint"
    assert d.file == "src/a.ts"
    assert d.line == 3
    assert d.code == "no-unused-vars"
    assert "never used" in d.message


def test_run_eslint_missing_rule_id(tmp_path):
    """eslint message without ruleId → code falls back to \"eslint\"."""
    repo = _ts_repo(tmp_path)
    bin_dir = repo / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    payload = json.dumps(
        [
            {
                "filePath": "src/a.ts",
                "messages": [{"severity": 2, "line": 1, "message": "Parsing error"}],
            }
        ]
    )
    _make_fake_tool(bin_dir, "eslint", f"#!/bin/sh\necho '{payload}'\n")
    diags = run_eslint("src/a.ts", repo)
    assert len(diags) == 1
    assert diags[0].code == "eslint"


def test_run_eslint_normalises_absolute_path(tmp_path):
    """eslint returns an absolute filePath → normalised to repo-relative."""
    repo = _ts_repo(tmp_path)
    bin_dir = repo / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    abs_path = str(repo / "src" / "a.ts")
    payload = json.dumps(
        [
            {
                "filePath": abs_path,
                "messages": [{"ruleId": "semi", "severity": 2, "line": 1, "message": ";"}],
            }
        ]
    )
    _make_fake_tool(bin_dir, "eslint", f"#!/bin/sh\necho '{payload}'\n")
    diags = run_eslint("src/a.ts", repo)
    assert len(diags) == 1
    assert diags[0].file == "src/a.ts"


# ── collect_static dispatch ──────────────────────────────────────────────────


def test_collect_static_py_still_works(tmp_path):
    """Python files still route to ruff + mypy (no regression)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "x.py").write_text("x = 1\n")
    result = collect_static("x.py", repo)
    assert isinstance(result, list)


def test_collect_static_ts_no_package_json(tmp_path):
    """TS file without package.json → empty, no tool attempted."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "a.ts").write_text("const x = 1;\n")
    result = collect_static("src/a.ts", repo)
    assert result == []


def test_collect_static_ts_no_tools(tmp_path):
    """TS file with package.json but no tsc/eslint → empty."""
    repo = _ts_repo(tmp_path)
    result = collect_static("src/a.ts", repo)
    assert result == []


def test_collect_static_ts_integration(tmp_path):
    """TS file with fake tsc + eslint → combined diagnostics."""
    repo = _ts_repo(tmp_path)
    bin_dir = repo / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    _make_fake_tool(
        bin_dir,
        "tsc",
        '#!/bin/sh\necho "src/a.ts(1,7): error TS2322: type mismatch"\n',
    )
    eslint_payload = json.dumps(
        [
            {
                "filePath": "src/a.ts",
                "messages": [{"ruleId": "semi", "severity": 2, "line": 1, "message": "Missing ;"}],
            }
        ]
    )
    _make_fake_tool(bin_dir, "eslint", f"#!/bin/sh\necho '{eslint_payload}'\n")
    result = collect_static("src/a.ts", repo)
    assert len(result) == 2
    tools = {d.tool for d in result}
    assert tools == {"tsc", "eslint"}


def test_cache_key_separates_py_from_ts(tmp_path):
    """Python and TS cache entries live under different tool-group keys."""
    repo = _ts_repo(tmp_path)
    bin_dir = repo / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    _make_fake_tool(bin_dir, "tsc", '#!/bin/sh\necho ""\n')
    _make_fake_tool(bin_dir, "eslint", '#!/bin/sh\necho "[]"\n')

    _CACHE.clear()
    collect_static("src/a.ts", repo)
    keys = list(_CACHE.keys())
    # tsc is project-wide and must NOT be in the per-file cache — only eslint is.
    assert any("eslint" in str(k) for k in keys), f"keys: {keys}"
    assert not any("tsc" in str(k) for k in keys), f"keys: {keys}"
    assert not any("ruff+mypy" in str(k) for k in keys)
    _CACHE.clear()


def test_all_ts_extensions_trigger_ts_path(tmp_path):
    """Every supported extension routes to tsc+eslint."""
    for ext in [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"]:
        repo = tmp_path / f"repo-{ext.lstrip('.')}"
        repo.mkdir()
        (repo / "package.json").write_text("{}")
        (repo / "src").mkdir()
        fname = f"src/file{ext}"
        (repo / fname).write_text("const x = 1;\n")
        bin_dir = repo / "node_modules" / ".bin"
        bin_dir.mkdir(parents=True)
        _make_fake_tool(bin_dir, "tsc", '#!/bin/sh\necho ""\n')
        _make_fake_tool(bin_dir, "eslint", '#!/bin/sh\necho "[]"\n')
        _CACHE.clear()
        result = collect_static(fname, repo)
        assert isinstance(result, list), f"failed for {ext}"
        keys = list(_CACHE.keys())
        assert any("eslint" in str(k) for k in keys), f"no eslint key for {ext}: {keys}"
        assert not any("tsc" in str(k) for k in keys), f"tsc must not be per-file cached: {keys}"


def test_collect_static_unknown_extension(tmp_path):
    """Unknown extension → empty list."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "f.rb").write_text("puts :hi\n")
    assert collect_static("f.rb", repo) == []


def test_collect_static_missing_file(tmp_path):
    """Missing file → empty list, no crash."""
    repo = tmp_path / "repo"
    repo.mkdir()
    assert collect_static("nope.py", repo) == []


def test_collect_static_caches_and_reuses(tmp_path):
    """Second call with same (path, mtime, size) hits the cache."""
    repo = _ts_repo(tmp_path)
    bin_dir = repo / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    _make_fake_tool(
        bin_dir,
        "tsc",
        '#!/bin/sh\necho "src/a.ts(1,1): error TS9999: cached"\n',
    )
    _make_fake_tool(bin_dir, "eslint", '#!/bin/sh\necho "[]"\n')

    _CACHE.clear()
    r1 = collect_static("src/a.ts", repo)
    assert len(r1) == 1
    # Replace tsc with a version that would produce different output.
    _make_fake_tool(
        bin_dir,
        "tsc",
        '#!/bin/sh\necho "src/a.ts(2,2): error TS0000: should-not-run"\n',
    )
    r2 = collect_static("src/a.ts", repo)
    # Must return the cached result, not re-run.
    assert r2 == r1
    _CACHE.clear()


def test_ts_diagnostics_refresh_when_a_sibling_file_changes(tmp_path, monkeypatch):
    """A change to types.ts must refresh diagnostics on an unchanged a.ts.

    tsc compiles the whole project. The old per-file cache returned a.ts's last
    result on (path, mtime, size) alone, so a type change in an imported file
    never reached the model — exactly the stale-fact failure the cache was
    supposed to avoid.
    """
    repo = _ts_repo(tmp_path)
    (repo / "types.ts").write_text("export type T = number\n")
    bin_dir = repo / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    tsc = bin_dir / "tsc"
    # The fake tool's output depends on the *project*, not on a.ts.
    tsc.write_text(
        "#!/bin/sh\n"
        'if grep -q BAD "$REPO_DIR/types.ts"; then '
        'echo "src/a.ts(1,1): error TS2322: bad type"; fi\n'
    )
    tsc.chmod(tsc.stat().st_mode | stat.S_IEXEC)
    _make_fake_tool(bin_dir, "eslint", '#!/bin/sh\necho "[]"\n')
    monkeypatch.setenv("REPO_DIR", str(repo))

    first = collect_static("src/a.ts", repo)
    assert first == []
    (repo / "types.ts").write_text("export type T = BAD\n")
    second = collect_static("src/a.ts", repo)
    assert any(d.tool == "tsc" for d in second), "tsc must rerun on a sibling change"


def test_source_stamp_detects_equal_count_file_swaps(tmp_path):
    """Same file count and newest mtime must not mean 'unchanged'."""
    from codeerratum.evidence.static import _source_stamp

    repo = _ts_repo(tmp_path)
    (repo / "old.ts").write_text("export const x = 1\n")
    os.utime(repo / "old.ts", (1_000_000_000, 1_000_000_000))
    before = _source_stamp(repo)

    (repo / "old.ts").unlink()
    (repo / "new.ts").write_text("export const y = 1\n")
    os.utime(repo / "new.ts", (1_000_000_000, 1_000_000_000))
    after = _source_stamp(repo)

    assert before[0] == after[0], "file count is unchanged by an equal swap"
    assert before[1] == after[1], "newest mtime is unchanged by an old-timestamp swap"
    assert before[2] != after[2], "the digest must still detect the rename"


def test_cache_prune_evicts_oldest(tmp_path):
    """When _CACHE reaches _CACHE_LIMIT, the oldest half is evicted."""
    from codeerratum.evidence.static import _CACHE_LIMIT

    _CACHE.clear()
    # Fill past the limit.
    for i in range(_CACHE_LIMIT + 10):
        _CACHE[("/fake", f"grp{i}", i, 0)] = []
    _cache_prune()
    assert len(_CACHE) <= _CACHE_LIMIT // 2 + 10  # evicted oldest half + remaining newer entries
    _CACHE.clear()


def test_tsc_runs_once_for_the_whole_project(tmp_path):
    """The cost invariant: N changed files must not mean N whole-project compiles.

    ``tsc --noEmit`` type-checks the project, so memoising it per file made a
    2-file ask compile twice, and 60 units would compile 60 times, in parallel.
    """
    repo = _ts_repo(tmp_path)
    (repo / "src" / "b.ts").write_text("const y = 2;\n")
    bin_dir = repo / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    counter = repo / "invocations"
    _make_fake_tool(
        bin_dir,
        "tsc",
        (
            "#!/bin/sh\n"
            f'printf x >> "{counter}"\n'
            'echo "src/a.ts(1,1): error TS1111: first"\n'
            'echo "src/b.ts(2,2): error TS2222: second"\n'
        ),
    )
    reset_tsc_cache()
    assert [d.code for d in run_tsc("src/a.ts", repo)] == ["TS1111"]
    assert [d.code for d in run_tsc("src/b.ts", repo)] == ["TS2222"]
    assert counter.read_text() == "x", "the second file must reuse the first compile"

    # collect_static goes through the same memo, and the per-file cache would
    # have hidden a second compile behind a different key.
    _CACHE.clear()
    collect_static("src/b.ts", repo)
    assert counter.read_text() == "x"


def test_tsc_cache_invalidates_on_source_edit(tmp_path):
    """A long-lived server must not hand the model last run's type errors.

    tsconfig/package.json alone cannot see an edit to a source file, which is the
    only thing the desktop app and the Web server change between two reviews.
    """
    repo = _ts_repo(tmp_path)
    bin_dir = repo / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    counter = repo / "invocations"
    _make_fake_tool(
        bin_dir,
        "tsc",
        (f'#!/bin/sh\nprintf x >> "{counter}"\necho "src/a.ts(1,1): error TS1111: first"\n'),
    )
    reset_tsc_cache()
    run_tsc("src/a.ts", repo)
    run_tsc("src/a.ts", repo)
    assert counter.read_text() == "x", "the repeat call is the cached one"

    source = repo / "src" / "a.ts"
    source.write_text("const x: string = 1;\n")
    # mtime_ns can still land in the same tick on a fast filesystem
    os.utime(source, ns=(source.stat().st_atime_ns, source.stat().st_mtime_ns + 1_000_000))
    run_tsc("src/a.ts", repo)
    assert counter.read_text() == "xx", "editing a source file must force a re-check"


def test_tsc_normalises_absolute_and_dotted_paths(tmp_path):
    """tsc/eslint path styles still have to match unit.primary_file."""
    repo = _ts_repo(tmp_path)
    bin_dir = repo / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    _make_fake_tool(
        bin_dir,
        "tsc",
        (
            "#!/bin/sh\n"
            'echo "./src/a.ts(3,1): error TS1000: relative dot"\n'
            f'echo "{repo}/src/a.ts(4,1): error TS1001: absolute"\n'
        ),
    )
    reset_tsc_cache()
    assert [d.code for d in run_tsc("src/a.ts", repo)] == ["TS1000", "TS1001"]


def test_staticdiag_to_dict(tmp_path):
    """StaticDiag.to_dict() serialises correctly."""
    d = StaticDiag(tool="tsc", file="a.ts", line=3, code="TS1234", message="bad")
    assert d.to_dict() == {
        "tool": "tsc",
        "file": "a.ts",
        "line": 3,
        "code": "TS1234",
        "message": "bad",
        "severity": "error",
    }


def test_known_facts_header_names_the_tools_that_ran():
    """The header used to hard-code ruff/mypy, which lied to a TS reviewer.

    A block that says "already reported by ruff/mypy" above tsc/eslint findings
    attributes the diagnostics to tools that were never run, and the model cannot
    correct a wrong fact stated inside its own instructions.
    """
    diags = [
        StaticDiag(tool="tsc", file="a.ts", line=3, code="TS2345", message="type"),
        StaticDiag(tool="eslint", file="a.ts", line=9, code="no-unused-vars", message="unused"),
    ]
    block = known_facts_block(diags)
    assert "eslint, tsc" in block
    assert "ruff" not in block
    assert "mypy" not in block

    py = [StaticDiag(tool="ruff", file="m.py", line=1, code="E501", message="long")]
    assert "ruff" in known_facts_block(py)
    assert "mypy" not in known_facts_block(py)
