"""Lifetime of the local server: stop it cleanly, or when its owner dies.

A desktop shell (pywebview, Tauri, an IDE task runner) owns the child process in
the happy path only. When the shell crashes, is force-quit, or gets ``kill -9``,
the server survives: it keeps a port bound and — worse — may still be running a
paid multi-model review whose result nobody will ever read.

``--parent-pid`` arms a cheap poll as a backstop. It is deliberately
dependency-free: one syscall every couple of seconds, no IPC protocol, no
heartbeat socket to misconfigure.

POSIX and Windows need different probes, and the Windows one is not obvious, so
both live here rather than in the CLI:

* POSIX: a dead parent makes us a child of init (or of the nearest subreaper),
  so ``getppid()`` changing is a definitive signal — but only if the process we
  were told to watch really was our direct parent at startup. ``kill(pid, 0)``
  alone would call a zombie "alive", which is exactly the crashed-shell case.
* Windows: ``getppid()`` is cached at interpreter start and never changes, and
  ``os.kill(pid, 0)`` is not a probe there — it terminates the process. Use
  ``OpenProcess`` + ``WaitForSingleObject`` with a zero timeout instead.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from collections.abc import Callable

# Seconds between liveness probes. Slow enough to be free, fast enough that an
# orphaned server does not outlive a crash by more than a couple of seconds.
DEFAULT_INTERVAL = 2.0


def parent_alive(pid: int, *, direct_child: bool | None = None) -> bool:
    """True while `pid` still exists.

    `direct_child` says whether `pid` was our parent when the watchdog started.
    Pass it explicitly when polling repeatedly: it is what makes the reparenting
    check trustworthy. When None it is derived from the current ``getppid()``,
    which is only correct on the first call.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _windows_alive(pid)

    if direct_child is None:
        direct_child = os.getppid() == pid
    if direct_child and os.getppid() != pid:
        # Reparented: our direct parent exited (a zombie would still be our
        # parent, so this also covers the crash-and-not-yet-reaped case).
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # It exists, it just belongs to someone else.
        return True
    except OSError:
        return False
    return True


def _windows_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # Explicit signatures: the default c_int return would truncate a handle on
    # 64-bit Windows and every later call would fail against a bad handle.
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    synchronize = 0x00100000
    wait_timeout = 0x00000102
    error_access_denied = 5

    handle = kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        # Access denied means it exists but is not ours to open (an elevated
        # parent). Treat that as alive: killing a healthy session is worse than
        # leaving a rare orphan behind.
        return ctypes.get_last_error() == error_access_denied
    try:
        return kernel32.WaitForSingleObject(handle, 0) == wait_timeout
    finally:
        kernel32.CloseHandle(handle)


def watch_parent(
    pid: int,
    on_dead: Callable[[], None],
    *,
    interval: float = DEFAULT_INTERVAL,
) -> threading.Thread:
    """Call `on_dead` once `pid` disappears. Returns the daemon thread.

    The first probe runs immediately, so a launcher that dies during our startup
    does not leave a server behind for one full interval.
    """
    direct = os.getppid() == pid if sys.platform != "win32" else None

    def loop() -> None:
        while parent_alive(pid, direct_child=direct):
            time.sleep(interval)
        on_dead()

    thread = threading.Thread(target=loop, name="codeerratum-parent-watch", daemon=True)
    thread.start()
    return thread


def stop_server(httpd, *, timeout: float = 5.0) -> bool:
    """Make ``serve_forever`` return. False if it did not stop in time.

    ``shutdown()`` blocks until the loop notices, and it deadlocks when called
    from the loop's own thread — a watchdog callback or a signal handler runs
    outside it, but neither is the loop, so the call always goes through a
    helper thread.
    """
    done = threading.Event()

    def _stop() -> None:
        try:
            httpd.shutdown()
        finally:
            done.set()

    threading.Thread(target=_stop, name="codeerratum-stop", daemon=True).start()
    return done.wait(timeout)


def defer_signals(on_signal: Callable[[int], None], names=("SIGTERM", "SIGINT")) -> bool:
    """Handle these signals from a thread instead of the main thread.

    A desktop window owns the main thread inside the platform event loop
    (Cocoa/GTK), where no Python bytecode runs — so a ``signal.signal`` handler
    sits unexecuted until the window closes, which is exactly when it is no
    longer needed. Blocking the signals and dequeuing them with ``sigwait`` in a
    dedicated thread works regardless of what the main thread is doing.

    POSIX only; returns False on Windows and when the primitives are missing, so
    the caller can fall back to an ordinary handler. Call this **before**
    starting threads: new threads inherit the blocked mask, existing ones keep
    theirs and could swallow the signal.
    """
    if sys.platform == "win32":
        return False
    wanted = {getattr(signal, name) for name in names if hasattr(signal, name)}
    if not wanted:
        return False
    try:
        signal.pthread_sigmask(signal.SIG_BLOCK, wanted)
    except (AttributeError, OSError, ValueError):
        return False

    def loop() -> None:
        while True:
            try:
                received = signal.sigwait(wanted)
            except OSError:  # interrupted syscall; nothing was dequeued
                continue
            on_signal(received)
            return  # one teardown is enough

    threading.Thread(target=loop, name="codeerratum-signals", daemon=True).start()
    return True
