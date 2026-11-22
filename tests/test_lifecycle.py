"""Server lifetime — graceful stop, and the orphan case a crash leaves behind."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

from codeerratum.web import lifecycle
from codeerratum.web.bootstrap import build
from codeerratum.web.lifecycle import parent_alive, stop_server, watch_parent


def test_this_process_is_alive():
    assert parent_alive(os.getpid()) is True


def test_unusable_pids_count_as_dead():
    # A launcher that passes 0, a negative, or a stale huge pid must not make the
    # watchdog spin: "no owner" means "stop", not "assume it is fine".
    assert parent_alive(0) is False
    assert parent_alive(-1) is False
    assert parent_alive(2**31 - 1) is False


def test_reaped_child_counts_as_dead():
    """kill(pid, 0) alone calls a zombie alive; a reaped child must read as gone."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert parent_alive(child.pid, direct_child=False) is True
    finally:
        child.terminate()
        child.wait(timeout=10)
    assert parent_alive(child.pid, direct_child=False) is False


def test_watch_fires_when_the_parent_is_already_gone(monkeypatch):
    monkeypatch.setattr(lifecycle, "parent_alive", lambda pid, **kw: False)
    fired = threading.Event()
    watch_parent(4242, fired.set, interval=0.01)
    assert fired.wait(2.0), "watchdog never noticed the parent was gone"


def test_watch_keeps_polling_while_the_parent_lives(monkeypatch):
    probes: list[int] = []

    def fake(pid, **kw):
        probes.append(pid)
        return len(probes) < 3  # alive twice, then gone

    monkeypatch.setattr(lifecycle, "parent_alive", fake)
    fired = threading.Event()
    watch_parent(7, fired.set, interval=0.01)
    assert fired.wait(2.0)
    assert len(probes) >= 3


def test_stop_server_releases_a_running_loop(tmp_path):
    _hub, httpd, _app = build(None, port=0, state_file=tmp_path / "projects.json")
    loop = threading.Thread(target=httpd.serve_forever, daemon=True)
    loop.start()
    try:
        # shutdown() from the serving thread itself would deadlock; stop_server
        # hands it to a helper thread, which is the only reason this returns.
        assert stop_server(httpd, timeout=5.0) is True
        loop.join(5.0)
        assert not loop.is_alive()
    finally:
        httpd.server_close()


def test_stop_server_reports_a_loop_that_refuses_to_stop():
    class Stuck:
        def shutdown(self):
            time.sleep(30)

    assert stop_server(Stuck(), timeout=0.05) is False


def test_defer_signals_falls_back_when_the_primitive_is_missing(monkeypatch):
    """A platform without pthread_sigmask must get "use an ordinary handler",
    not an exception on the way to opening the window."""

    def unavailable(*args, **kwargs):
        raise AttributeError("pthread_sigmask")

    monkeypatch.setattr(lifecycle.signal, "pthread_sigmask", unavailable, raising=False)
    assert lifecycle.defer_signals(lambda signum: None) is False
