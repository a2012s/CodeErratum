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
from pathlib import Path

from ..i18n import t
from .lifecycle import stop_server

DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 860
# Below this the three-column layout (tabs, list, progress) stops being usable
# and the CSS media query would switch to the narrow layout in a desktop window.
DEFAULT_MIN_SIZE = (980, 640)
STORAGE_DIR = Path.home() / ".codeerratum" / "webview"


def open_window(
    url: str,
    title: str,
    *,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    min_size: tuple[int, int] = DEFAULT_MIN_SIZE,
) -> None:
    """Open the window and block until the user closes it."""
    try:
        import webview
    except ImportError as exc:  # optional extra, or a platform without a GUI
        raise RuntimeError(t("desk.missing")) from exc

    webview.create_window(
        title,
        url,
        width=width,
        height=height,
        min_size=min_size,
        # Default is False, which makes findings and paths unselectable — the
        # whole point of the report page is copying lines out of it.
        text_select=True,
    )
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
    for window in list(getattr(webview, "windows", None) or []):
        try:
            window.destroy()
        except Exception:  # noqa: BLE001 - a closing window may already be gone
            pass


def launch(httpd, url: str, title: str, *, window=None, **options) -> None:
    """Serve on a background thread while the window owns the main thread.

    The order matters on macOS: AppKit requires the GUI on the main thread, so
    the server goes to a daemon thread and ``window()`` blocks here. When the
    window closes, the server is stopped and the port released before returning.
    """
    window = window or open_window
    thread = threading.Thread(target=httpd.serve_forever, name="codeerratum-ui", daemon=True)
    thread.start()
    try:
        window(url, title, **options)
    finally:
        stop_server(httpd)
        thread.join(timeout=5.0)
        httpd.server_close()
