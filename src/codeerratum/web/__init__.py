"""Local web UI (optional). Standard library only — no extra dependencies."""

from .server import WebApp, make_handler, make_server

__all__ = ["WebApp", "make_handler", "make_server"]
