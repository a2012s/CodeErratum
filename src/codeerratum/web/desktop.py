"""Native desktop window around the unchanged local UI.

The window is a thin shell on purpose. Every behaviour stays in the Python
server; the webview only loads ``http://127.0.0.1:<port>/``.

That last part is not a detail. The server's ``_origin_ok`` guard requires the
request ``Origin`` to equal the loopback ``Host`` exactly, so a page served from
a custom protocol (``file://``, ``tauri://``, an asset proxy) would get 403 on
every POST — the review button would silently do nothing. Loading the real
loopback URL keeps the origin honest and needs no relaxation of the guard.

pywebview drives the system WebView (WKWebView / WebView2 / WebKitGTK), so the
bundle is a Python environment plus the OS runtime — no Chromium copy.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from ..i18n import t
from .lifecycle import stop_server

DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 860
# Below this the three-column layout (tabs, list, progress) stops being usable
# and the CSS media query would switch to the narrow layout in a desktop window.
DEFAULT_MIN_SIZE = (980, 640)
STORAGE_DIR = Path.home() / ".codeerratum" / "webview"

# Set by close_window() (watchdog / SIGTERM) so the closing handler knows this
# is a forced teardown, not a user click. A forced close must never wait on a
# dialog: nobody is there to answer it, and the parent is already gone.
_FORCED = threading.Event()


def guard_close_while_busy(window, is_busy: Callable[[], bool]) -> Callable[[], bool | None]:
    """Build a ``closing`` handler that asks before killing a paid run.

    NEVER call ``window.create_confirmation_dialog()`` from here. On macOS its
    implementation posts the NSAlert to the main runloop via ``callAfter`` and
    then blocks on a semaphore — but the closing event itself is dispatched
    synchronously from ``applicationShouldTerminate_``/``windowShouldClose_``
    **on that same main thread**. The alert waits for a runloop that is blocked
    on the alert: a guaranteed deadlock (0.5.0 hang, 43 s unresponsive).

    The safe path is pywebview's own ``confirm_close`` property: its
    ``should_close()`` reads it right after the closing handlers and, when set,
    runs ``NSAlert.runModal()`` inline on the main thread — no semaphore, no
    self-wait. The handler only flips the flag and returns ``None`` so the
    built-in flow continues. The check is a callback, not a snapshot: a run can
    start after the window opens.
    """
    def _on_closing() -> bool | None:
        window.confirm_close = bool(is_busy()) and not _FORCED.is_set()
        return None

    return _on_closing


def open_window(
    url: str,
    title: str,
    *,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    min_size: tuple[int, int] = DEFAULT_MIN_SIZE,
    is_busy: Callable[[], bool] | None = None,
) -> None:
    """Open the window and block until the user closes it."""
    try:
        import webview
    except ImportError as exc:  # optional extra, or a platform without a GUI
        raise RuntimeError(t("desk.missing")) from exc

    window = webview.create_window(
        title,
        url,
        width=width,
        height=height,
        min_size=min_size,
        # Default is False, which makes findings and paths unselectable — the
        # whole point of the report page is copying lines out of it.
        text_select=True,
        # The built-in should_close() uses these strings when confirm_close is
        # flipped by our closing handler. Overriding the defaults keeps the
        # question about *reviews* rather than pywebview's generic quit text.
        localization={
            "global.quitConfirmation": (
                t("desk.close_busy_title") + "\n\n" + t("desk.close_busy_message")
            ),
            "global.quit": t("desk.close_busy_quit"),
            "global.cancel": t("desk.close_busy_cancel"),
        },
    )
    if is_busy is not None:
        # Only the user's close button / Cmd+Q goes through this handler.
        # The watchdog and signal teardown call window.destroy() directly, so
        # a crash or a parent exit can never be blocked by a confirmation box.
        window.events.closing += guard_close_while_busy(window, is_busy)
    # private_mode (the default) drops the WebView profile on exit, and the UI
    # keeps the chosen language and the pane widths in localStorage — those
    # would reset on every launch. A fixed storage_path keeps them.
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    webview.start(private_mode=False, storage_path=str(STORAGE_DIR))


def close_window() -> None:
    """Close the GUI from another thread (watchdog, SIGTERM).

    Stopping the HTTP server is not enough: ``webview.start()`` owns the main
    thread until the window closes, so a signal that only stopped the loop would
    leave a window open over a dead server.
    """
    try:
        import webview
    except ImportError:
        return
    _FORCED.set()
    for window in list(getattr(webview, "windows", None) or []):
        try:
            window.destroy()
        except Exception:  # noqa: BLE001 - a closing window may already be gone
            pass


def launch(
    httpd,
    url: str,
    title: str,
    *,
    window=None,
    is_busy: Callable[[], bool] | None = None,
    **options,
) -> None:
    """Serve on a background thread while the window owns the main thread.

    The order matters on macOS: AppKit requires the GUI on the main thread, so
    the server goes to a daemon thread and ``window()`` blocks here. When the
    window closes, the server is stopped and the port released before returning.
    """
    window = window or open_window
    if is_busy is not None:
        options["is_busy"] = is_busy
    thread = threading.Thread(target=httpd.serve_forever, name="codeerratum-ui", daemon=True)
    thread.start()
    try:
        window(url, title, **options)
    finally:
        stop_server(httpd)
        thread.join(timeout=5.0)
        httpd.server_close()
