"""Wire a project hub and a loopback server.

Kept out of the CLI so the desktop launcher, the tests and any future embedding
all build the server the same way. The CLI still owns user-facing validation:
by the time :func:`build` is called the repository is known to exist (or the
caller explicitly asked for none).
"""

from __future__ import annotations

from http.server import ThreadingHTTPServer
from pathlib import Path

from .hub import ProjectHub
from .server import WebApp, make_server


def default_state_file() -> Path:
    """Where restored projects come back from.

    The desktop app has no current working directory to inherit, so this file is
    its entire notion of "what was open". A function, not a constant: a frozen
    build and a test can both have a different home directory than the one
    resolved at import time.
    """
    return Path.home() / ".codeerratum" / "projects.json"


def build(
    repo_root: Path | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    max_parallel: int = 2,
    state_file: Path | None = None,
) -> tuple[ProjectHub, ThreadingHTTPServer, WebApp | None]:
    """Return ``(hub, httpd, root_app)``.

    ``repo_root=None`` starts with an empty project list — the desktop case,
    where the window opens on the overview and the user adds repositories from
    there. ``root_app`` is then the first restored project, or None when the hub
    is empty and ``/api/*`` has nothing to answer for.

    ``port=0`` lets the OS choose; read ``httpd.server_address[1]`` for the port
    that was actually granted.
    """
    hub = ProjectHub(
        state_file=default_state_file() if state_file is None else state_file,
        max_parallel=max_parallel,
    )
    # Restore first, seed second. The other order wiped the saved list: add()
    # writes the state file with only the new repo, and restore() then reads
    # that file back — so `ui --repo B` quietly dropped every project the
    # desktop app had open. Seeding is an addition, never a replacement.
    hub.restore()  # previously added projects come back after a restart
    seeded = hub.add(str(repo_root)) if repo_root is not None else None
    app = hub.get(seeded["slug"]) if seeded is not None else hub.default_app()
    httpd = make_server(app, host=host, port=port, hub=hub)
    return hub, httpd, app
