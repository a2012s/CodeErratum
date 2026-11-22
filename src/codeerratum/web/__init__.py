"""Local web UI (optional). Standard library only — no extra dependencies."""

from .hub import ProjectHub, slug_for
from .server import WebApp, make_handler, make_server

__all__ = ["ProjectHub", "WebApp", "make_handler", "make_server", "slug_for"]
