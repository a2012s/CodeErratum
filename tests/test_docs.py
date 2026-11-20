"""Docs generation and drift tests.

The one behaviour worth defending: drift means the *signature* changed, not that the
file moved. A check that flags every refactor gets switched off within a week, so line
numbers are recorded but never compared.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codeerratum.cli import app
from codeerratum.docs import (
    build_index,
    collect,
    compare,
    extract_module,
    load_index,
    render_module,
    save_index,
    write_all,
)

MODULE = '''
"""Demo module."""


def public_fn(a: int, b: str = "x") -> bool:
    """Return whether a and b agree.

    Longer explanation that should not appear in the summary.
    """
    return str(a) == b


async def async_fn(x):
    return x


def _private_fn():
    return 1


class Widget(Base, metaclass=Meta):
    """A widget."""

    def render(self) -> str:
        """Draw it."""
        return ""

    def _internal(self):
        return 2
'''


def _write(tmp_path, text: str = MODULE, name: str = "demo.py"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# ---- extraction ---------------------------------------------------------


def test_signature_includes_defaults_and_return_type(tmp_path):
    module = extract_module(_write(tmp_path), tmp_path)
    fn = next(s for s in module.symbols if s.qualname == "public_fn")
    assert fn.signature == "def public_fn(a: int, b: str='x') -> bool"
    assert fn.kind == "function"
    assert fn.is_public is True


def test_async_is_marked_and_class_signature_keeps_bases(tmp_path):
    module = extract_module(_write(tmp_path), tmp_path)
    afn = next(s for s in module.symbols if s.qualname == "async_fn")
    assert afn.signature.startswith("async def")
    widget = next(s for s in module.symbols if s.qualname == "Widget")
    assert widget.kind == "class"
    assert widget.signature == "class Widget(Base, metaclass=Meta)"


def test_methods_are_nested_under_their_class(tmp_path):
    module = extract_module(_write(tmp_path), tmp_path)
    widget = next(s for s in module.symbols if s.qualname == "Widget")
    names = [c.qualname for c in widget.children]
    assert "Widget.render" in names
    assert next(c for c in widget.children if c.qualname == "Widget.render").kind == "method"


def test_docstring_summary_is_the_first_paragraph_only(tmp_path):
    module = extract_module(_write(tmp_path), tmp_path)
    fn = next(s for s in module.symbols if s.qualname == "public_fn")
    assert fn.docstring == "Return whether a and b agree."
    assert "Longer explanation" not in fn.docstring


def test_private_symbols_are_marked_not_public(tmp_path):
    module = extract_module(_write(tmp_path), tmp_path)
    private = next(s for s in module.symbols if s.qualname == "_private_fn")
    assert private.is_public is False


def test_dunder_methods_count_as_public():
    from codeerratum.docs.extract import _is_public

    assert _is_public("__init__") is True
    assert _is_public("_helper") is False
    assert _is_public("normal") is True


def test_unparseable_file_is_skipped_not_fatal(tmp_path):
    assert extract_module(_write(tmp_path, "def broken(:\n", "bad.py"), tmp_path) is None


def test_collect_skips_build_and_cache_dirs(tmp_path):
    _write(tmp_path, name="keep.py")
    for junk in (".venv", "__pycache__", "node_modules"):
        d = tmp_path / junk
        d.mkdir()
        (d / "skip.py").write_text("x = 1\n", encoding="utf-8")
    modules = collect(tmp_path)
    assert [m.path for m in modules] == ["keep.py"]


def test_package_init_maps_to_the_package_name(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    _write(pkg, name="__init__.py")
    modules = collect(tmp_path)
    assert modules[0].module == "pkg"


def test_scanning_src_drops_the_src_prefix(tmp_path):
    """Docs should say `codeerratum.cli`, not `src.codeerratum.cli`."""
    pkg = tmp_path / "src" / "pkg"
    pkg.mkdir(parents=True)
    _write(pkg, name="__init__.py")
    modules = collect(tmp_path, tmp_path / "src")
    assert modules[0].module == "pkg"
    assert modules[0].path == "src/pkg/__init__.py"


# ---- drift --------------------------------------------------------------


def _index(tmp_path, text: str) -> dict:
    module = extract_module(_write(tmp_path, text), tmp_path)
    assert module is not None
    return build_index([module])


def test_no_change_means_no_drift(tmp_path):
    before = _index(tmp_path, MODULE)
    after = _index(tmp_path, MODULE)
    assert compare(before, after) == []


def test_signature_change_is_drift(tmp_path):
    before = _index(tmp_path, MODULE)
    after = _index(tmp_path, MODULE.replace("def public_fn(a: int", "def public_fn(a: int, c: int"))
    kinds = [d.kind for d in compare(before, after)]
    assert kinds == ["signature_changed"]
    assert compare(before, after)[0].is_breaking is True


def test_moving_code_is_not_drift(tmp_path):
    """A check that fires on every refactor is a check nobody keeps using."""
    before = _index(tmp_path, MODULE)
    shifted = "\n\n" + MODULE
    after = _index(tmp_path, shifted)
    assert compare(before, after) == []


def test_private_signature_change_is_ignored(tmp_path):
    before = _index(tmp_path, MODULE)
    after = _index(tmp_path, MODULE.replace("def _private_fn():", "def _private_fn(y):"))
    assert compare(before, after) == []


def test_removed_and_added_symbols_are_reported(tmp_path):
    before = _index(tmp_path, MODULE)
    after = _index(tmp_path, MODULE.replace("def public_fn", "def renamed_fn"))
    kinds = sorted(d.kind for d in compare(before, after))
    assert kinds == ["symbol_added", "symbol_removed"]
    removed = next(d for d in compare(before, after) if d.kind == "symbol_removed")
    assert removed.is_breaking is True
    assert removed.path == "demo.py"


def test_module_removal_is_reported(tmp_path):
    before = _index(tmp_path, MODULE)
    (tmp_path / "demo.py").unlink()
    after = build_index([])
    assert [d.kind for d in compare(before, after)] == ["module_removed"]


def test_index_roundtrip_and_version_guard(tmp_path):
    path = tmp_path / "idx.json"
    save_index(path, _index(tmp_path, MODULE))
    assert load_index(path) is not None
    broken = json.loads(path.read_text())
    broken["version"] = 99
    path.write_text(json.dumps(broken), encoding="utf-8")
    assert load_index(path) is None


def test_missing_index_is_not_an_error(tmp_path):
    assert load_index(tmp_path / "nope.json") is None


# ---- rendering ----------------------------------------------------------


def test_render_lists_public_and_hides_private(tmp_path):
    module = extract_module(_write(tmp_path), tmp_path)
    text = render_module(module, commit="abcdef1234")
    assert "`public_fn`" in text
    assert "`Widget.render`" in text
    assert "_private_fn" not in text
    assert "abcdef12" in text
    assert "def public_fn" in text


def test_render_flags_missing_docstring(tmp_path):
    module = extract_module(_write(tmp_path, "def bare():\n    return 1\n"), tmp_path)
    assert "无 docstring" in render_module(module)


def test_write_all_produces_one_file_per_module_plus_overview(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _write(src, name="one.py")
    modules = collect(tmp_path)
    out = tmp_path / "api"
    written = write_all(modules, out, commit="c")
    assert len(written) == len(modules) + 1
    assert (out / "src" / "one.md").is_file()
    assert (out / "README.md").is_file()
    assert "src.one" in (out / "README.md").read_text(encoding="utf-8")


# ---- CLI ----------------------------------------------------------------


@pytest.fixture
def lonely_repo():
    """A temp dir outside any git repository, so find_repo_root stays put."""
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


def test_check_without_baseline_exits_nonzero(lonely_repo):
    (lonely_repo / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["docs", "--repo", str(lonely_repo), "--check"])
    assert result.exit_code == 1
    assert "尚无基线" in result.stdout


def test_check_is_clean_right_after_generating(lonely_repo):
    (lonely_repo / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    runner = CliRunner()
    assert runner.invoke(app, ["docs", "--repo", str(lonely_repo)]).exit_code == 0
    result = runner.invoke(app, ["docs", "--repo", str(lonely_repo), "--check", "--strict"])
    assert result.exit_code == 0
    assert "无签名漂移" in result.stdout


def test_strict_check_fails_after_a_breaking_change(lonely_repo):
    source = lonely_repo / "m.py"
    source.write_text("def f(a):\n    return a\n", encoding="utf-8")
    runner = CliRunner()
    runner.invoke(app, ["docs", "--repo", str(lonely_repo)])
    source.write_text("def f(a, b):\n    return a\n", encoding="utf-8")
    result = runner.invoke(app, ["docs", "--repo", str(lonely_repo), "--check", "--strict"])
    assert result.exit_code == 1
    assert "签名变更" in result.stdout
