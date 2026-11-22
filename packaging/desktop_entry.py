"""Frozen-build entry point — what a double-clicked .app actually runs.

PyInstaller needs a script, not a console command, so this is the thin adapter
that turns "launch me" into ``codeerratum desktop``. It also carries the two
things a windowed build has no other way to do:

* **A log file.** A windowed build has no usable stdout on Windows and nothing
  visible on macOS, so a startup failure would look like "the icon bounced and
  nothing happened". Everything goes to ``~/.codeerratum/desktop.log`` and a
  fatal error also raises a native alert.
* **``--selftest``.** Verifies the data files the review pipeline reads at
  runtime (``prompts/*.md``) and the HTML assets are actually inside the bundle.
  Missing them is invisible at startup — the app opens fine and only fails when
  someone spends tokens. The build script runs this against the frozen binary.

Run it from source too (``python packaging/desktop_entry.py --selftest``); it
behaves the same, minus the log redirect.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

LOG_PATH = Path.home() / ".codeerratum" / "desktop.log"

# Kept in sync with packaging/CodeErratum.spec: the module pywebview loads for a
# window on this platform. The selftest imports it, because "import webview"
# succeeds even when the backend's dependencies were left out of the bundle.
BACKENDS = {
    "darwin": "webview.platforms.cocoa",
    "win32": "webview.platforms.edgechromium",
}


def _redirect_output() -> None:
    """Point stdout/stderr at a file. Keeps prints from crashing a GUI process."""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 1_000_000:
            LOG_PATH.replace(LOG_PATH.with_suffix(".log.1"))  # keep one generation
        handle = open(LOG_PATH, "a", buffering=1, encoding="utf-8")
    except OSError:
        return  # read-only home: better a silent log than no window
    sys.stdout = handle
    sys.stderr = handle
    # rich wraps to $COLUMNS; a log read after the fact is easier to scan wide.
    os.environ.setdefault("COLUMNS", "120")
    print(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} codeerratum desktop ===")


def _alert(message: str) -> None:
    """Best-effort native dialog; the log file is the fallback, not this."""
    text = message.replace('"', "'")[:600]
    try:
        if sys.platform == "darwin":
            subprocess.run(
                ["osascript", "-e", f'display alert "CodeErratum" message "{text}"'],
                capture_output=True,
                timeout=30,
                check=False,
            )
        elif sys.platform == "win32":
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, text, "CodeErratum", 0x10)
        else:
            subprocess.run(
                ["zenity", "--error", "--title=CodeErratum", f"--text={text}"],
                capture_output=True,
                timeout=30,
                check=False,
            )
    except Exception:  # noqa: BLE001 - a failed alert must not mask the real error
        pass


def selftest() -> int:
    """Check the files a frozen build silently loses."""
    from codeerratum import __version__
    from codeerratum.review import executor, falsification
    from codeerratum.web import server

    checks = [
        ("static/index.html", server.STATIC_DIR / "index.html", False),
        ("static/projects.html", server.STATIC_DIR / "projects.html", False),
        ("prompts/", executor.PROMPT_PATH.parent, True),
        ("prompts/falsify.md", falsification.PROMPT_PATH, False),
    ]
    problems = []
    for label, path, want_dir in checks:
        ok = path.is_dir() if want_dir else path.is_file()
        print(f"{'ok  ' if ok else 'MISS'} {label}: {path}")
        if not ok:
            problems.append(label)

    try:
        import webview

        print(f"ok   webview: {Path(webview.__file__).parent}")
    except Exception as exc:  # noqa: BLE001
        print(f"MISS webview: {exc}")
        problems.append("webview")

    # pywebview resolves its backend with importlib at window-creation time, so
    # "import webview" proves nothing: the failure it hides is a missing backend
    # dependency (pythonnet/.NET assemblies on Windows), which only shows up when
    # the first window is about to open — after the user has launched the app.
    backend = BACKENDS.get(sys.platform, "webview.platforms.gtk")
    try:
        __import__(backend)
        print(f"ok   backend: {backend}")
    except Exception as exc:  # noqa: BLE001
        print(f"MISS backend: {backend}: {type(exc).__name__}: {exc}")
        problems.append(backend)

    print(f"version: {__version__} | frozen: {getattr(sys, 'frozen', False)}")
    print("SELFTEST", "FAILED: " + ", ".join(problems) if problems else "OK")
    return 1 if problems else 0


def desktop_argv(args: list[str]) -> list[str]:
    """Command line for a double-clicked launch.

    A double-clicked app has no repository: launched from Finder the working
    directory is "/", so seeding from cwd would abort with "not a git
    repository" before a window ever appears. Start on the overview and let the
    saved project list (or the user) supply one — unless a repo was asked for.
    """
    if any(arg == "--repo" or arg.startswith("--repo=") for arg in args):
        return ["codeerratum", "desktop", *args]
    return ["codeerratum", "desktop", "--no-repo", *args]


def main() -> int:
    argv = sys.argv[1:]
    frozen = getattr(sys, "frozen", False)
    # A windowed build on Windows has no stdout at all — the selftest included.
    # Redirecting before the check means a failed build leaves a readable trail
    # in desktop.log instead of exit code 1 next to an empty CI log.
    if "--selftest" in argv:
        if frozen and sys.stdout is None:
            _redirect_output()
        return selftest()
    if frozen:
        _redirect_output()

    sys.argv = desktop_argv(argv)
    try:
        from codeerratum.cli import app

        app()
    except SystemExit as exc:  # typer's normal exit path
        return int(exc.code or 0)
    except BaseException:  # noqa: BLE001 - nothing else will report this
        detail = traceback.format_exc()
        print(detail)
        if frozen:
            _alert(f"启动失败，详情见 {LOG_PATH}\n\n{detail.strip().splitlines()[-1]}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
