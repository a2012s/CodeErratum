"""Desktop launcher — frozen asset lookup, an empty hub, and window lifecycle.

The window itself needs a GUI and a real display, so it is injected: what is
worth testing is that the server is up before the window loads, the shell's
URLs answer, and closing the window actually releases the port.
"""

from __future__ import annotations

import json
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from codeerratum import cli
from codeerratum.i18n import language
from codeerratum.web import bootstrap, desktop, server
from codeerratum.web.lifecycle import stop_server


def make_bundle(tmp_path: Path, layout: str = "codeerratum/web/static") -> Path:
    root = tmp_path / "bundle"
    static = root / layout
    static.mkdir(parents=True)
    (static / "index.html").write_text("<!doctype html><html></html>", encoding="utf-8")
    (static / "projects.html").write_text("<!doctype html><html></html>", encoding="utf-8")
    return root


@contextmanager
def serving(httpd) -> Iterator[str]:
    loop = threading.Thread(target=httpd.serve_forever, daemon=True)
    loop.start()
    host, port = httpd.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        stop_server(httpd)
        loop.join(5.0)
        httpd.server_close()


# ---- frozen asset lookup --------------------------------------------------


def test_static_dir_is_found_under_meipass(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    assert server._locate_static() == bundle / "codeerratum" / "web" / "static"


def test_static_dir_accepts_the_shallower_add_data_layout(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path, layout="web/static")
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    assert server._locate_static() == bundle / "web" / "static"


def test_static_dir_falls_back_to_the_package(tmp_path, monkeypatch):
    # A wrong --add-data must degrade to "templates not found", not a crash at
    # import time: the in-tree copy is the last resort even when frozen.
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "empty"), raising=False)
    found = server._locate_static()
    assert (found / "index.html").is_file()
    assert "static" in found.name


# ---- bare start, no repository -------------------------------------------


def build_bare(tmp_path):
    with language("zh"):
        return bootstrap.build(None, port=0, state_file=tmp_path / "projects.json")


def test_bare_start_serves_the_overview_shell(tmp_path):
    _hub, httpd, app = build_bare(tmp_path)
    assert app is None
    with serving(httpd) as base:
        with httpx.Client(base_url=base, timeout=10, trust_env=False) as c:
            page = c.get("/")
            assert page.status_code == 200
            assert "projects" in page.text.lower()
            listing = c.get("/api/projects")
            assert listing.status_code == 200
            assert listing.json()["projects"] == []


def test_bare_start_explains_the_missing_project_instead_of_500(tmp_path):
    _hub, httpd, _app = build_bare(tmp_path)
    with serving(httpd) as base:
        with httpx.Client(base_url=base, timeout=10, trust_env=False) as c:
            r = c.get("/api/meta")
            assert r.status_code == 404  # not 500: nothing is broken, nothing is selected
            assert "还没有添加项目" in r.text


def test_bare_start_does_not_seed_the_saved_list(tmp_path):
    state = tmp_path / "projects.json"
    state.write_text('{"projects": ["/nope/gone"]}', encoding="utf-8")
    with language("zh"):
        hub, httpd, app = bootstrap.build(None, port=0, state_file=state)
    try:
        assert app is None  # the saved path vanished, so the hub stays empty
        assert hub.snapshot()["projects"] == []
    finally:
        httpd.server_close()


# ---- window lifecycle -----------------------------------------------------


def test_launch_serves_before_the_window_and_releases_the_port_after(tmp_path):
    _hub, httpd, _app = build_bare(tmp_path)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    seen: dict = {}

    def fake_window(url, title, **options):
        seen["url"] = url
        seen["title"] = title
        with httpx.Client(timeout=10, trust_env=False) as c:
            seen["status"] = c.get(url + "api/projects").status_code

    desktop.launch(httpd, base + "/", "CodeErratum", window=fake_window)

    assert seen["url"] == base + "/"
    assert seen["status"] == 200
    with pytest.raises(httpx.HTTPError):
        httpx.get(base + "/api/projects", timeout=2, trust_env=False)


def test_launch_stops_the_server_when_the_window_fails(tmp_path):
    _hub, httpd, _app = build_bare(tmp_path)

    def boom(url, title, **options):
        raise RuntimeError("no display")

    with pytest.raises(RuntimeError, match="no display"):
        desktop.launch(httpd, "http://127.0.0.1:1/", "t", window=boom)
    # If this hangs or errors, the port was left bound by a failed launch.
    httpd.server_close()


def test_missing_gui_extra_points_at_the_optional_dependency(monkeypatch):
    monkeypatch.setitem(sys.modules, "webview", None)  # `import webview` -> ImportError
    with pytest.raises(RuntimeError, match="pywebview"):
        desktop.open_window("http://127.0.0.1:1/", "t")


# ---- launcher handshake ---------------------------------------------------


def test_print_port_emits_one_machine_readable_line(tmp_path, capsys):
    _hub, httpd, _app = build_bare(tmp_path)
    try:
        url = cli._url_for(httpd, "127.0.0.1", True)
        port = httpd.server_address[1]
    finally:
        httpd.server_close()
    assert url == f"http://127.0.0.1:{port}/"
    assert capsys.readouterr().out.strip() == f"PORT={port}"


def test_non_loopback_host_is_warned_not_silent(capsys):
    """The UI has no auth; --host 0.0.0.0 hands it to the LAN. The Host/Origin
    checks only stop browser rebinding, so an explicit bind deserves a warning."""
    cli._warn_non_loopback("0.0.0.0")
    out = capsys.readouterr().out
    assert "--host 0.0.0.0" in out
    for loopback in ("127.0.0.1", "localhost", "::1"):
        cli._warn_non_loopback(loopback)
    assert capsys.readouterr().out == ""


def test_launcher_flags_are_exposed():
    runner = CliRunner()
    ui_help = " ".join(runner.invoke(cli.app, ["ui", "--help"]).output.split())
    assert "--no-repo" in ui_help
    assert "--print-port" in ui_help
    assert "--parent-pid" in ui_help
    desk_help = " ".join(runner.invoke(cli.app, ["desktop", "--help"]).output.split())
    assert "--parent-pid" in desk_help
    assert "--width" in desk_help


# ---- frozen entry point ---------------------------------------------------


def load_entry():
    """Import packaging/desktop_entry.py by path.

    `import packaging` would pick up the setuptools helper of the same name from
    site-packages, so the file is loaded explicitly instead.
    """
    import importlib.util

    path = Path(__file__).resolve().parent.parent / "packaging" / "desktop_entry.py"
    spec = importlib.util.spec_from_file_location("ce_desktop_entry", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_double_click_starts_without_a_repository():
    """Finder launches with cwd=/ — seeding from cwd would abort before the
    window opens, so the entry point must ask for the overview instead."""
    entry = load_entry()
    assert entry.desktop_argv([]) == ["codeerratum", "desktop", "--no-repo"]
    assert entry.desktop_argv(["--width", "1000"]) == [
        "codeerratum",
        "desktop",
        "--no-repo",
        "--width",
        "1000",
    ]


def test_explicit_repository_is_not_overridden():
    entry = load_entry()
    assert entry.desktop_argv(["--repo", "/tmp/x"]) == [
        "codeerratum",
        "desktop",
        "--repo",
        "/tmp/x",
    ]
    assert entry.desktop_argv(["--repo=/tmp/x"]) == [
        "codeerratum",
        "desktop",
        "--repo=/tmp/x",
    ]


def test_seeding_a_repo_keeps_the_projects_already_saved(tmp_path):
    """`ui --repo B` used to replace the whole saved list with B.

    add() wrote the state file before restore() read it, so seeding was a
    destructive overwrite: the desktop app opened later and found one project.
    Seeding has to be an addition.
    """
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    state = tmp_path / "projects.json"
    state.write_text(json.dumps({"projects": [str(first)]}), encoding="utf-8")

    hub, httpd, app = bootstrap.build(second, port=0, state_file=state)
    try:
        assert [p["name"] for p in hub.snapshot()["projects"]] == ["first", "second"]
        assert app is not None
    finally:
        httpd.server_close()
    assert len(json.loads(state.read_text(encoding="utf-8"))["projects"]) == 2


# ---- closing guard: a paid run must not die without a question ----------------


class _FakeClosingEvent:
    def __init__(self):
        self.handlers: list = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


class _FakeWindow:
    def __init__(self):
        self.events = type("Events", (), {"closing": _FakeClosingEvent()})()
        self.confirm_close = False


@pytest.fixture(autouse=True)
def _clear_forced_close():
    """Forced-close state is module-level; never leak it between tests."""
    desktop._FORCED.clear()
    yield
    desktop._FORCED.clear()


def test_close_guard_only_asks_while_busy():
    """The handler flips confirm_close and never calls a blocking dialog.

    Regression (0.5.0 hang): calling create_confirmation_dialog() from a
    closing handler deadlocks macOS — the dialog waits for the main runloop
    that the handler itself is blocking. A fake without that method proves the
    handler stays on the safe path.
    """
    win = _FakeWindow()
    busy = [False]
    guard = desktop.guard_close_while_busy(win, lambda: busy[0])

    assert guard() is None, "handler never decides the close itself"
    assert win.confirm_close is False, "idle close needs no question"
    busy[0] = True
    assert guard() is None
    assert win.confirm_close is True, "busy close hands off to confirm_close"
    busy[0] = False
    assert guard() is None
    assert win.confirm_close is False, "flag must reset for the next close"


def test_forced_close_never_asks(monkeypatch):
    """Watchdog/SIGTERM teardown must not stop at a dialog nobody can answer."""
    win = _FakeWindow()
    guard = desktop.guard_close_while_busy(win, lambda: True)
    desktop._FORCED.set()
    assert guard() is None
    assert win.confirm_close is False


def test_open_window_registers_the_closing_guard(monkeypatch, tmp_path):
    win = _FakeWindow()
    captured: dict = {}

    def fake_create_window(title, url, **kw):
        captured.update(kw)
        return win

    fake_webview = type(sys)("webview")
    fake_webview.create_window = fake_create_window
    fake_webview.start = lambda **kw: None
    monkeypatch.setitem(sys.modules, "webview", fake_webview)
    monkeypatch.setattr(desktop, "STORAGE_DIR", tmp_path / "storage")

    with language("zh"):
        desktop.open_window("http://127.0.0.1:1/", "t", is_busy=lambda: True)

    assert len(win.events.closing.handlers) == 1
    win.events.closing.handlers[0]()
    assert win.confirm_close is True
    # The built-in dialog's text must describe the review, not pywebview's
    # generic "Do you really want to quit?".
    loc = captured["localization"]
    assert "审查" in loc["global.quitConfirmation"]
    assert loc["global.quit"] == "退出并中断"
    assert loc["global.cancel"] == "继续审查"

    # Without a busy callback the shell stays exactly as before: no handler.
    plain = _FakeWindow()
    fake_webview.create_window = lambda *a, **kw: plain
    desktop.open_window("http://127.0.0.1:1/", "t")
    assert plain.events.closing.handlers == []


def test_launch_forwards_the_busy_callback(tmp_path):
    _hub, httpd, _app = build_bare(tmp_path)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    seen: dict = {}

    def fake_window(url, title, **options):
        seen["is_busy"] = options.get("is_busy")

    try:
        desktop.launch(
            httpd, base + "/", "t", window=fake_window, is_busy=lambda: True
        )
    finally:
        httpd.server_close()
    assert seen["is_busy"] is not None
    assert seen["is_busy"]() is True


def test_review_busy_covers_both_modes():
    from types import SimpleNamespace

    assert cli.review_busy(None, None) is False
    assert cli.review_busy(None, SimpleNamespace(review_status=lambda: {"running": False})) is False
    assert cli.review_busy(None, SimpleNamespace(review_status=lambda: {"running": True})) is True
    assert cli.review_busy(SimpleNamespace(snapshot=lambda: {"running": 0}), None) is False
    assert cli.review_busy(SimpleNamespace(snapshot=lambda: {"running": 2}), None) is True
