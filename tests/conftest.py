"""Shared test isolation.

The config resolution chain ends at ``~/.codeerratum/config.json``. Without this
fixture a developer's real roster would leak into every assertion about
``source == "env"`` / ``"none"``, and a test that saves a config would rewrite
the machine's actual default. Each test gets a throwaway home instead.
"""

from __future__ import annotations

import os

import pytest

#: Console width the suite renders at. typer draws ``--help`` through rich, and
#: rich folds a word that does not fit the line: at 20 columns ``--no-repo``
#: comes out as ``--no-`` plus ``repo``, and Chinese phrases get broken between
#: characters. The width comes from ``COLUMNS``, which a CI runner and a human
#: terminal set differently, so it is pinned here instead of inherited. Set
#: ``CODEERRATUM_TEST_COLUMNS`` to render at a different width on purpose.
TEST_COLUMNS = "120"

# Set at import time, not in a fixture, because rich freezes the width when a
# Console is constructed — and `codeerratum.cli` builds one at module level
# (`console = Console()`). A fixture would only take effect after that import
# had already happened, leaving the console at whatever width the process
# started with. pytest imports conftest.py before the test modules, so this is
# early enough.
os.environ["COLUMNS"] = os.environ.get("CODEERRATUM_TEST_COLUMNS", TEST_COLUMNS)


@pytest.fixture(autouse=True)
def isolated_user_home(tmp_path, monkeypatch):
    home = tmp_path / "user-home"
    monkeypatch.setenv("CODEERRATUM_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def pinned_console_width(monkeypatch):
    """Give every test the same console width, whatever the machine has."""
    monkeypatch.setenv("COLUMNS", TEST_COLUMNS)
    monkeypatch.delenv("TERMINAL_WIDTH", raising=False)
    return TEST_COLUMNS
