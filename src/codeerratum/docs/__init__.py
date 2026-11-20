"""Documentation generation and drift detection — deterministic, no model involved."""

from .extract import ModuleDoc, SymbolDoc, collect, extract_module
from .index import Drift, build_index, compare, load_index, save_index, summarize
from .render import render_module, render_overview, write_all

__all__ = [
    "Drift",
    "ModuleDoc",
    "SymbolDoc",
    "build_index",
    "collect",
    "compare",
    "extract_module",
    "load_index",
    "render_module",
    "render_overview",
    "save_index",
    "summarize",
    "write_all",
]
