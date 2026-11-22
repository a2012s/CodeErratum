"""Multi-project hub — many isolated project sessions behind one local server.

Each project gets its own :class:`WebApp`, so the review lock, progress state and
per-repo store/config are never shared. A hub-wide semaphore caps how many
projects may run a paid review at the same time (default 2): every project
already fans out to N models, so unbounded parallelism would trip provider rate
limits rather than finish faster.

The browser shell keeps one tab ("便签") per project, each an iframe pointing at
``/p/<slug>/``; the project page itself is the unchanged single-repo UI.
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

from ..pipeline import PipelineError
from .server import WebApp, pick_folder, resolve_git_root

DEFAULT_MAX_PARALLEL = 2


def slug_for(path: Path) -> str:
    """Stable, URL-safe id for a repo path (order-independent, no collisions)."""
    return hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:10]


class ProjectHub:
    """Ordered set of project sessions plus the shared execution gate."""

    def __init__(
        self,
        state_file: Path | None = None,
        max_parallel: int = DEFAULT_MAX_PARALLEL,
    ) -> None:
        self.state_file = state_file
        self.max_parallel = max(1, int(max_parallel))
        self._slots = threading.BoundedSemaphore(self.max_parallel)
        self._lock = threading.RLock()
        self._apps: dict[str, WebApp] = {}
        self._order: list[str] = []

    # ---- registration ----------------------------------------------------

    def add(self, raw_path: str) -> dict:
        value = str(raw_path or "").strip()
        if not value:
            raise PipelineError("请填写本地文件夹路径")
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            raise PipelineError("请填写绝对路径（支持 ~ 开头的家目录）")
        return self.add_root(resolve_git_root(candidate.resolve()))

    def add_root(self, root: Path) -> dict:
        slug = slug_for(root)
        with self._lock:
            if slug not in self._apps:
                self._apps[slug] = WebApp(root, gate=self._slots)
                self._order.append(slug)
                self._save()
            return self._entry(slug)

    def add_picked(self) -> dict:
        return self.add_root(pick_folder("选择要加入审查的项目"))

    def remove(self, slug: str) -> dict:
        with self._lock:
            app = self._apps.get(slug)
            if app is None:
                raise LookupError("项目不存在")
            if app.review_status()["running"]:
                raise RuntimeError("该项目正在审查，先停止再移除")
            del self._apps[slug]
            self._order.remove(slug)
            self._save()
        return {"removed": slug}

    # ---- lookup ----------------------------------------------------------

    def get(self, slug: str) -> WebApp:
        with self._lock:
            app = self._apps.get(slug)
        if app is None:
            raise LookupError("项目不存在")
        return app

    def default_app(self) -> WebApp | None:
        with self._lock:
            return self._apps[self._order[0]] if self._order else None

    def first_or_raise(self) -> WebApp:
        app = self.default_app()
        if app is None:
            raise PipelineError("还没有项目，请先添加一个本地仓库")
        return app

    # ---- views -----------------------------------------------------------

    def snapshot(self) -> dict:
        """Lightweight status for the shell's tabs and overview cards."""
        with self._lock:
            items = [(slug, self._apps[slug]) for slug in self._order]
        rows: list[dict] = []
        running = 0
        for slug, app in items:
            st = app.review_status()
            running += 1 if st["running"] else 0
            rows.append(
                {
                    "slug": slug,
                    "name": app.repo_root.name,
                    "path": str(app.repo_root),
                    "running": st["running"],
                    "status": st["status"],
                    "error": st.get("error", ""),
                    "elapsed_s": st.get("elapsed_s"),
                    "tokens": st.get("tokens"),
                    "params": st.get("params", {}),
                    "progress": (st.get("messages") or [""])[-1],
                }
            )
        return {
            "projects": rows,
            "max_parallel": self.max_parallel,
            "running": running,
            "slots": max(0, self.max_parallel - running),
        }

    def _entry(self, slug: str) -> dict:
        app = self._apps[slug]
        return {"slug": slug, "name": app.repo_root.name, "path": str(app.repo_root)}

    # ---- persistence -----------------------------------------------------

    def _save(self) -> None:
        if self.state_file is None:
            return
        payload = {
            "projects": [str(self._apps[slug].repo_root) for slug in self._order],
            "max_parallel": self.max_parallel,
        }
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.state_file)
        except OSError:
            # A read-only home must not break the session; the list is a cache.
            return

    def restore(self) -> int:
        """Reload the saved project list, silently skipping paths that vanished."""
        if self.state_file is None or not self.state_file.exists():
            return 0
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        paths = data.get("projects") if isinstance(data, dict) else None
        if not isinstance(paths, list):
            return 0
        added = 0
        for raw in paths:
            try:
                root = resolve_git_root(Path(str(raw)).expanduser().resolve())
            except (PipelineError, OSError, ValueError):
                continue
            before = len(self._order)
            self.add_root(root)
            added += 1 if len(self._order) > before else 0
        return added
