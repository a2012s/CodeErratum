"""Shared test isolation.

The config resolution chain ends at ``~/.codeerratum/config.json``. Without this
fixture a developer's real roster would leak into every assertion about
``source == "env"`` / ``"none"``, and a test that saves a config would rewrite
the machine's actual default. Each test gets a throwaway home instead.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_user_home(tmp_path, monkeypatch):
    home = tmp_path / "user-home"
    monkeypatch.setenv("CODEERRATUM_HOME", str(home))
    return home
