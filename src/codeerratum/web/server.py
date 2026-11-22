"""Local web UI — standard library only, bound to loopback.

No FastAPI, no bundler, no node_modules: `codeerratum ui` has to work on a
machine that has nothing installed but this package. ThreadingHTTPServer gives
us "page stays responsive while a review runs" for free; a lock keeps two
reviews from running at once (the same cost guard as `--max-units`).

Security posture: binds 127.0.0.1 by default, requires a same-origin loopback
Host AND Origin on every POST (a malicious page must not be able to trigger a
paid review through your browser, including via a DNS-rebound hostname), and
validates run ids / output paths before touching the filesystem.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .. import HOMEPAGE, __version__
from ..config_store import KEEP_SECRET
from ..config_store import load as load_config
from ..config_store import load_user as load_user_config
from ..config_store import public as public_config
from ..config_store import save as save_config_file
from ..i18n import set_lang, t
from ..ingest import DiffError, normalize_pathspec
from ..model_check import probe_model
from ..models import ModelRegistry
from ..pipeline import (
    AUTO_UNIT_BUDGET,
    HOUSE_RULES_FILE,
    STATE_DIR,
    NoChanges,
    PipelineError,
    ReviewCancelled,
    available_models,
    execute_review,
    find_repo_root,
    resolve_house_rules,
    store_for,
)
from ..review import MAX_HOUSE_RULES_CHARS


def _locate_static() -> Path:
    """Find the bundled HTML assets, frozen or from source.

    A frozen build (PyInstaller/py2app) has no ``__file__``-relative package
    directory: data files live under ``sys._MEIPASS`` and the layout depends on
    how ``--add-data`` was written. Probe the plausible roots and keep the
    in-tree path as the last resort so a mistake surfaces as a clear 404 rather
    than an import-time crash.
    """
    candidates: list[Path] = []
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        candidates += [
            Path(bundle) / "codeerratum" / "web" / "static",
            Path(bundle) / "web" / "static",
            Path(bundle) / "static",
        ]
    here = Path(__file__).parent / "static"
    candidates.append(here)
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    return here


STATIC_DIR = _locate_static()
RUN_ID_RE = re.compile(r"^[0-9a-f]{6,32}$")
FINDING_ID_RE = re.compile(r"^[0-9a-f]{6,32}$")
LOOPBACK_HOST_RE = re.compile(r"^(?:127\.0\.0\.1|localhost|\[::1\]|::1)(?::\d+)?$", re.I)
MAX_BODY = 2 * 1024 * 1024
# Cost guard for the web UI. The CLI trusts --max-units as given; the UI is a
# click-away surface (and reachable by any local page script), so an absurd value
# is clamped here — and the clamp is announced instead of applied silently.
MAX_UNITS_CAP = 500


def _write_sse(wfile, kind: str, payload: dict) -> bool:
    """Write one SSE frame. Returns False once the client has gone away.

    A closed tab must not abort a paid review: the run keeps going, other tabs
    (and a later reload) pick it up from ``/api/review/status``.
    """
    data = json.dumps({"type": kind, **payload}, ensure_ascii=False)
    try:
        wfile.write(f"event: {kind}\ndata: {data}\n\n".encode())
        wfile.flush()
    except OSError:  # BrokenPipe / ConnectionReset: the reader is gone
        return False
    return True


def resolve_git_root(candidate: Path) -> Path:
    """Validate a directory and snap it to its git root when it has one.

    A folder without ``.git`` is accepted: the review pipeline can snapshot the
    working tree, so requiring a repository first would refuse exactly the case
    people hit most — a project that has not been committed yet. When the folder
    does live inside a repository the root still wins, so one entry covers the
    whole work tree.
    """
    if not candidate.exists():
        raise PipelineError(t("server.path_missing", path=candidate))
    if not candidate.is_dir():
        raise PipelineError(t("server.not_a_folder", path=candidate))
    return find_repo_root(candidate)


def pick_folder(title: str | None = None) -> Path:
    """Open the native folder picker in this local process and return a git root.

    A browser page cannot read a directory dialog's absolute path, so the dialog
    must run here. Shared by single-repo switching and the multi-project hub.
    """
    title = title or t("server.picker_title")
    system = sys.platform
    if system == "darwin":
        cmd = ["osascript", "-e", f'POSIX path of (choose folder with prompt "{title}")']
    elif system == "win32":
        cmd = [
            "powershell",
            "-NoProfile",
            "-Command",
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
            f"$d.Description = '{title}'; "
            "if ($d.ShowDialog() -eq 'OK') { $d.SelectedPath }",
        ]
    else:
        zenity = shutil.which("zenity")
        if not zenity:
            raise PipelineError(t("server.picker_no_zenity"))
        cmd = [zenity, "--file-selection", "--directory", f"--title={title}"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)
    except subprocess.TimeoutExpired as exc:
        raise PipelineError(t("server.picker_timeout")) from exc
    except OSError as exc:
        raise PipelineError(t("server.picker_open_failed", error=exc)) from exc
    if result.returncode != 0 or not result.stdout.strip():
        # macOS -128 / "User canceled." — not an error worth a toast.
        raise PipelineError(t("server.picker_cancelled"))
    chosen = result.stdout.strip().rstrip(os.sep) or os.sep
    return resolve_git_root(Path(chosen).expanduser().resolve())


def _safe_error(exc: Exception, repo_root: Path) -> str:
    """Show the real reason in the browser without leaking local filesystem paths."""
    text = " ".join(str(exc).split())
    for path in (str(repo_root), str(Path.home())):
        if path:
            text = text.replace(path, "<repo>" if path == str(repo_root) else "~")
    return text[:400] or type(exc).__name__


def open_homepage() -> dict:
    """Open the project's GitHub page in the user's real browser.

    The URL is a constant, never a request field: this is a "show the project
    page" button, not a generic ``open any URL`` primitive a same-origin page
    could aim anywhere. It lives outside ``WebApp`` because it must work with
    zero projects open — the shell that shows About may not hold a repository,
    and the desktop webview cannot hand a URL to the real browser by itself.
    """
    try:
        opened = webbrowser.open(HOMEPAGE, new=2)
    except OSError as exc:  # no browser association at all
        raise PipelineError(t("server.open_failed", error=exc)) from exc
    if not opened:
        raise PipelineError(t("server.open_failed", error="no browser found"))
    return {"opened": True, "url": HOMEPAGE}


def _finding_brief(finding, model_id: str) -> dict:
    """The three fields a live list needs — not the whole work order.

    The full record (detail, snippet, suggestion, verdicts) is fetched with the
    finished run. Streaming it per unit would push kilobytes through the stream
    for a line of text the panel is going to truncate anyway.
    """
    anchor = finding.anchors[0] if finding.anchors else None
    return {
        "id": finding.id,
        "model": model_id,
        "severity": str(finding.severity),
        "lens": str(finding.lens),
        "claim": finding.claim,
        "file": anchor.file if anchor else "",
        "line": anchor.start_line if anchor else 0,
        "unit_id": finding.unit_id,
    }


class WebApp:
    """Request-independent application logic, so tests can call it without sockets."""

    def __init__(
        self,
        repo_root: Path,
        registry: ModelRegistry | None = None,
        gate: threading.BoundedSemaphore | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.registry = registry
        # Shared across projects by the hub: limits how many paid reviews run at
        # once. None keeps single-project behaviour (no queueing).
        self._gate = gate
        self._review_lock = threading.Lock()
        # Thread-safe stop flag. The orchestrator polls it from inside its own
        # asyncio loop and cancels in-flight model calls; a POST from any tab can
        # set it without touching that loop from the wrong thread.
        self._stop_event = threading.Event()
        # Shared progress state: any client can see the one running review, so a
        # second tab (or a stale request) knows the button must read "审查中".
        self._state_lock = threading.Lock()
        self._review_state: dict = {
            "running": False,
            "status": "idle",
            "messages": [],
            "started_at": None,
            "finished_at": None,
            "run_id": None,
            "params": {},
            "tokens": None,
            "error": "",
            # Findings seen so far, so a tab that joined mid-run (refresh, second
            # tab, run started elsewhere) can draw them too — the SSE connection
            # only reaches the request that opened it.
            "findings": [],
        }

    @property
    def store(self):
        return store_for(self.repo_root)

    # ---- reads -----------------------------------------------------------

    def models(self) -> dict:
        """The model picker's source of truth: ids, families, and seat counts.

        Keys never leave the process; the picker only needs names to choose from.
        """
        if self.registry is not None:
            rows = [
                {"id": m.model_id, "family": m.resolved_family(), "measured_n": m.measured_n}
                for m in self.registry.models
            ]
        else:
            rows = available_models(self.repo_root)
        return {
            "models": rows,
            "seats": {"quick": 1, "dual": 2, "committee": 3},
            "auto_budget": AUTO_UNIT_BUDGET,
        }

    def meta(self) -> dict:
        info = public_config(self.repo_root)
        info["repo"] = str(self.repo_root)
        info["state_dir"] = str(self.repo_root / STATE_DIR)
        # Cheap and fixed: the tab header shows the version, and the About dialog
        # reads the address from here rather than hard-coding it a third time.
        info["version"] = __version__
        info["homepage"] = HOMEPAGE
        info["house_rules"] = self.house_rules_info()
        # /api/meta keeps its original two-field chip shape; the settings
        # drawer reads /api/config for the rest (endpoints, keys masked, ...).
        info["models"] = [
            {"model_id": m["model_id"], "family": m["resolved_family"]} for m in info["models"]
        ]
        # An explicitly injected registry (tests, embedding) still takes
        # precedence for the run itself; reflect that in the chips too.
        if self.registry is not None:
            info["configured"] = True
            info["models"] = [
                {"model_id": m.model_id, "family": m.resolved_family()}
                for m in self.registry.models
            ]
        return info

    def config_info(self) -> dict:
        """Full (masked) configuration for the settings drawer."""
        return public_config(self.repo_root)

    def _git_root(self, candidate: Path) -> Path:
        """Resolve a path to its git root, or raise with a clear message."""
        return resolve_git_root(candidate)

    def switch_repo(self, raw_path: str) -> dict:
        """Point the UI at another local git repository.

        Three ways in: paste an absolute path, use the native folder picker
        (`pick_repo`), or drop a folder on the page (`find_repo_by_name`). All
        funnel through here so validation and the mid-review guard live in one
        place. The browser can never read a folder picker's absolute path, so
        the native dialog runs in this local process instead.
        """
        value = str(raw_path or "").strip()
        if not value:
            raise PipelineError(t("server.repo_path_required"))
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            raise PipelineError(t("server.repo_abs_required"))
        root = self._git_root(candidate.resolve())
        if not self._review_lock.acquire(blocking=False):
            raise RuntimeError(t("server.switch_blocked"))
        try:
            self.repo_root = root
        finally:
            self._review_lock.release()
        return self.meta()

    def pick_repo(self) -> dict:
        """Open the OS folder picker in this local process; return the choice.

        A browser page cannot obtain an absolute path from a file/directory
        dialog — that would let any site enumerate the disk. Running the native
        dialog here (the server is a local, loopback-only process) closes that
        gap while still giving the user a real folder browser. Cancel returns a
        clear message rather than a stack trace.
        """
        root = pick_folder()
        return {"path": str(root), "repo": str(root)}

    def open_repo_folder(self) -> dict:
        """Show the current repository in the OS file manager.

        The target always comes from the server's resolved git root, never from
        the request body — a same-origin page must not be able to use this as a
        generic "open any folder" primitive. Explorer may report a non-zero
        status after opening a window, so its status is not treated as failure.
        """
        target = self.repo_root
        if not target.is_dir():
            raise PipelineError(t("server.repo_dir_missing", path=target))
        system = sys.platform
        if system == "darwin":
            cmd = ["open", str(target)]
        elif system == "win32":
            cmd = ["explorer", str(target)]
        else:
            opener = shutil.which("xdg-open") or shutil.which("gio")
            if not opener:
                raise PipelineError(t("server.opener_missing"))
            cmd = (
                [opener, "open", str(target)]
                if Path(opener).name == "gio"
                else [opener, str(target)]
            )
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False)
        except subprocess.TimeoutExpired as exc:
            raise PipelineError(t("server.open_timeout")) from exc
        except OSError as exc:
            raise PipelineError(t("server.open_failed", error=exc)) from exc
        if result.returncode != 0 and system != "win32":
            lines = (result.stderr or result.stdout or "").strip().splitlines()
            raise PipelineError(
                t("server.open_failed", error=lines[-1] if lines else t("server.open_unknown"))
            )
        return {"path": str(target), "opened": True}

    def find_repo_by_name(self, raw_name: str, hints: list[str] | None = None) -> dict:
        """Resolve a dropped folder name to git repos on disk.

        A drag-and-drop `File`/`DirectoryEntry` exposes only the leaf name, never
        an absolute path. Search a bounded set of likely roots (recent repos,
        the current repo's neighbourhood, the home directory) so dropping a
        folder still lands on the right repository without a disk-wide walk.
        """
        name = str(raw_name or "").strip().strip("/\\")
        if not name or name in (".", "..") or "/" in name or "\\" in name:
            raise PipelineError(t("server.dropped_name_invalid"))
        candidates: list[Path] = []
        seen: set[Path] = set()

        def consider(p: Path) -> None:
            resolved = p.resolve()
            if resolved in seen or not resolved.is_dir():
                return
            seen.add(resolved)
            if resolved.name == name and (resolved / ".git").exists():
                candidates.append(resolved)

        for hint in (hints or [])[:6]:
            try:
                hint_path = Path(str(hint)).expanduser().resolve()
                consider(hint_path / name)
                consider(hint_path.parent / name)
            except (OSError, ValueError):
                continue
        consider(self.repo_root.parent / name)

        # Bounded descent from home: hidden dirs, caches and dependency trees
        # are pruned so this stays fast on a real machine.
        home = Path.home()
        max_depth, deadline = 5, time.monotonic() + 4.0
        skip = {
            "Library",
            "node_modules",
            ".git",
            ".venv",
            "venv",
            "__pycache__",
            "site-packages",
            ".cache",
            ".npm",
            ".cargo",
            ".Trash",
            "Applications",
        }
        for current, dirs, _files in os.walk(home, topdown=True, followlinks=False):
            if time.monotonic() > deadline or len(candidates) >= 20:
                break
            base = Path(current)
            depth = len(base.relative_to(home).parts)
            dirs[:] = [
                d for d in dirs if not d.startswith(".") and d not in skip and depth < max_depth
            ]
            for d in dirs:
                consider(base / d)
        return {"name": name, "matches": [str(p) for p in candidates[:20]]}

    def save_config(self, payload: dict) -> dict:
        return save_config_file(self.repo_root, payload)

    def check_model(self, payload: dict) -> dict:
        """Probe a single model row, resolving ``__KEEP__`` against saved config.

        The row may be edited but not yet saved, so its typed key is what gets
        tested. ``__KEEP__`` means "keep the stored key" — resolve it first, or
        the probe would test with no key and report a false red cross.
        """
        api_key = str(payload.get("api_key") or "").strip()
        if api_key == KEEP_SECRET:
            api_key = ""
            try:
                # A project with no file of its own is reading the default
                # roster; the masked placeholder the user sees came from there.
                saved = load_config(self.repo_root) or load_user_config()
            except (OSError, ValueError):
                saved = None
            if saved:
                model_id = str(payload.get("model_id") or "").strip()
                api_key = next(
                    (m.api_key for m in saved.registry.models if m.model_id == model_id), ""
                )
        return probe_model(
            model_id=str(payload.get("model_id") or ""),
            base_url=str(payload.get("base_url") or ""),
            api_key=api_key,
        )

    def house_rules_info(self) -> dict:
        """Report the auto-discovered rules file so an empty textarea is not ambiguous."""
        return resolve_house_rules(None, self.repo_root).summary() | {"path": HOUSE_RULES_FILE}

    def docs_overview(self) -> dict:
        """Module map plus signature drift, computed from the AST on each request.

        Parsing a few dozen files is cheaper than keeping a cache coherent with edits.
        """
        from ..docs import build_index, collect, compare, load_index
        from ..ingest import get_commit

        scan = (self.repo_root / "src") if (self.repo_root / "src").is_dir() else None
        try:
            modules = collect(self.repo_root, scan)
        except (NotADirectoryError, OSError) as exc:
            raise PipelineError(f"cannot scan sources: {exc}") from exc

        commit = get_commit(self.repo_root)
        current = build_index(modules, commit=commit)
        baseline = load_index(self.repo_root / STATE_DIR / "docs-index.json")
        drifts = compare(baseline, current) if baseline else []
        return {
            "commit": commit,
            "has_baseline": baseline is not None,
            "baseline_commit": (baseline or {}).get("commit", ""),
            "modules": [
                {
                    "module": m.module,
                    "path": m.path,
                    "public": sum(1 for s in m.flatten() if s.is_public),
                    "total": len(m.flatten()),
                }
                for m in modules
            ],
            "drift": [
                {
                    "kind": d.kind,
                    "path": d.path,
                    "symbol": d.symbol,
                    "detail": d.detail,
                    "breaking": d.is_breaking,
                }
                for d in drifts
            ],
        }

    def list_runs(self) -> dict:
        rows = self.store.list_runs()
        for row in rows:
            artifacts = self.repo_root / STATE_DIR / "runs" / row["run_id"]
            row["has_artifact"] = (artifacts / "findings.json").exists()
        return {"runs": rows}

    def get_run(self, run_id: str) -> dict:
        if not RUN_ID_RE.match(run_id):
            raise LookupError("invalid run id")
        run_dir = self.repo_root / STATE_DIR / "runs" / run_id
        findings_path = run_dir / "findings.json"
        if not findings_path.exists():
            raise LookupError(f"run {run_id} has no findings.json (artifacts pruned?)")
        collection = json.loads(findings_path.read_text(encoding="utf-8"))
        findings = collection.get("findings", [])
        ids = [f["id"] for f in findings]
        statuses = self.store.statuses_for(ids)
        for f in findings:
            state = statuses.get(f["id"], {})
            f["status"] = state.get("status", f.get("status", "open"))
            f["rejection_reason"] = state.get("reason", "")

        mode = collection.get("mode", "quick")
        zones = {"all": findings} if mode == "quick" else _split_zones(findings, mode)
        runs = {r["run_id"]: r for r in self.store.list_runs(limit=200)}
        row = runs.get(run_id, {})
        return {
            "run_id": run_id,
            "repo": collection.get("repo", str(self.repo_root)),
            "commit": collection.get("commit", ""),
            "mode": mode,
            "created_at": row.get("created_at", ""),
            "models": collection.get("reviewer_models", []),
            "falsifier": collection.get("falsifier_model"),
            "comparison": collection.get("comparison", {}),
            "notes": collection.get("notes", []),
            "spec_level": collection.get("spec_level", "L1"),
            "house_rules": {
                "applied": bool(collection.get("house_rules_sha")),
                "sha": collection.get("house_rules_sha", ""),
                "chars": collection.get("house_rules_chars", 0),
                "source": collection.get("house_rules_source", ""),
            },
            "totals": {
                "findings": len(findings),
                "rejected": len(collection.get("rejected", [])),
            },
            "zones": zones,
            "rejected": collection.get("rejected", []),
            "has_markdown": (run_dir / "review.md").exists(),
        }

    def read_markdown(self, run_id: str) -> str:
        if not RUN_ID_RE.match(run_id):
            raise LookupError("invalid run id")
        path = self.repo_root / STATE_DIR / "runs" / run_id / "review.md"
        if not path.exists():
            raise LookupError("review.md not found for this run")
        return path.read_text(encoding="utf-8")

    def stats(self) -> dict:
        return self.store.stats()

    def review_status(self) -> dict:
        """Snapshot of the single in-flight review (or the last finished one).

        Polled by every open tab so the run button and progress panel reflect a
        review started elsewhere — including a request whose SSE connection was
        dropped but whose server-side work is still going.
        """
        with self._state_lock:
            state = dict(self._review_state)
            state["messages"] = list(state["messages"])
            state["findings"] = list(state.get("findings") or [])
            state["params"] = dict(state["params"])
            if state["started_at"] is not None and state["finished_at"] is None:
                state["elapsed_s"] = round(time.monotonic() - state["started_at"], 1)
            else:
                state["elapsed_s"] = None
            # monotonic clock has no wall-clock meaning over HTTP; expose a readable start.
            state["started_at"] = (
                time.strftime("%H:%M:%S", time.localtime(state["started_at_wall"]))
                if state.get("started_at_wall")
                else ""
            )
            state["finished_at"] = (
                time.strftime("%H:%M:%S", time.localtime(state["finished_at_wall"]))
                if state.get("finished_at_wall")
                else ""
            )
            state.pop("started_at_wall", None)
            state.pop("finished_at_wall", None)
            return state

    def _note_progress(self, message: str) -> None:
        with self._state_lock:
            msgs = self._review_state["messages"]
            msgs.append(message)
            # Bound memory for long dual/committee runs with many units.
            if len(msgs) > 400:
                del msgs[:-400]

    def _note_finding(self, brief: dict) -> None:
        """Remember a live finding for everyone who is not on the SSE connection.

        The same finding is reported once per model, so the identity is
        ``id + model``; two models reporting the same claim are two rows until the
        run's clustering merges them, which is exactly what the panel claims.
        """
        key = (brief.get("id"), brief.get("model"))
        with self._state_lock:
            items = self._review_state.setdefault("findings", [])
            if any((i.get("id"), i.get("model")) == key for i in items):
                return
            items.append(brief)
            # Bound it like the message log: a committee run can produce hundreds.
            if len(items) > 500:
                del items[:-500]

    # ---- writes ----------------------------------------------------------

    def prepare_review(self, payload: dict) -> dict:
        """Validate + normalize a review request. Does not take the lock.

        Kept separate from execution so the SSE route can reject bad input with
        a normal 400 before it has committed to sending an event stream.
        """
        diff_spec = str(payload.get("diff") or "HEAD~1").strip() or "HEAD~1"
        mode = str(payload.get("mode") or "").strip().lower()
        raw_units = payload.get("max_units", "auto")
        auto_units = raw_units is None or (
            isinstance(raw_units, str) and raw_units.strip().lower() in ("", "auto")
        )
        # None → let the pipeline decide from the unit count. 0 → every unit.
        # A positive number is a budget and stays clamped, but never silently: the
        # clamp rides the progress log so the run explains how many units it covers.
        max_units: int | None = None
        notices: list[str] = []
        if not auto_units:
            try:
                requested_units = int(raw_units)
            except (TypeError, ValueError) as exc:
                raise PipelineError("max_units must be an integer, 0 or 'auto'") from exc
            if requested_units == 0:
                max_units = 0
            else:
                max_units = max(1, min(requested_units, MAX_UNITS_CAP))
                if max_units != requested_units:
                    key = (
                        "server.units_clamped_low"
                        if requested_units < 1
                        else "server.units_clamped_high"
                    )
                    notices.append(t(key, asked=requested_units, used=max_units, cap=MAX_UNITS_CAP))
        author = str(payload.get("author_model") or "").strip() or None
        # Validate here so a bad path is a 400 with a clear message, not a
        # git failure buried in a 500 after the review has started.
        try:
            paths = normalize_pathspec(str(payload.get("paths") or ""))
        except DiffError as exc:
            raise PipelineError(str(exc)) from exc
        # Rules arrive as text, never as a path: the UI must not be able to read
        # arbitrary local files, even though it only listens on loopback.
        raw_models = payload.get("models")
        models: list[str] = []
        if isinstance(raw_models, list):
            models = [str(m).strip() for m in raw_models if str(m).strip()]
        elif isinstance(raw_models, str) and raw_models.strip():
            models = [part.strip() for part in raw_models.split(",") if part.strip()]
        if models:
            # A typo in a model id must be a 400 at submit time, not a 500 once the
            # diff has been read and the run has started.
            known = {row["id"] for row in self.models()["models"]}
            unknown = [mid for mid in models if mid not in known]
            if unknown:
                raise PipelineError(t("server.unknown_models", ids=", ".join(unknown)))
        house_rules = str(payload.get("house_rules") or "")
        if len(house_rules) > MAX_HOUSE_RULES_CHARS * 2:
            raise PipelineError(
                f"house_rules is too long ({len(house_rules)} chars, "
                f"limit {MAX_HOUSE_RULES_CHARS * 2})"
            )
        out: Path | None = None
        if payload.get("write_markdown"):
            raw_out = str(payload.get("out") or "erratumReview.md")
            # execute_review resolves this against the repo root. Without a
            # plain-filename check, "../../.ssh/authorized_keys" or an
            # absolute path would be a legal target for a local page to write.
            if "/" in raw_out or "\\" in raw_out or raw_out in (".", "..", ""):
                raise PipelineError("out must be a plain filename in the repository root")
            out = Path(raw_out)
        return {
            "diff_spec": diff_spec,
            "mode": mode,
            "max_units": max_units,
            "models": models,
            "author": author,
            "out": out,
            "house_rules": house_rules,
            "paths": paths,
            "notices": notices,
        }

    def acquire_review_lock(self, params: dict | None = None) -> None:
        if not self._review_lock.acquire(blocking=False):
            raise RuntimeError(t("server.review_busy"))
        self._stop_event.clear()
        with self._state_lock:
            self._review_state.update(
                {
                    "running": True,
                    "status": "running",
                    "messages": [],
                    "started_at": time.monotonic(),
                    "started_at_wall": time.time(),
                    "finished_at": None,
                    "run_id": None,
                    "params": {
                        "diff": (params or {}).get("diff_spec", ""),
                        "mode": (params or {}).get("mode", ""),
                        "max_units": (params or {}).get("max_units"),
                        "paths": (params or {}).get("paths", ""),
                        "notices": (params or {}).get("notices", []),
                    },
                    "tokens": None,
                    "error": "",
                    # A new run starts with an empty live list; otherwise the
                    # panel would show the previous run's findings as if they
                    # had just been found.
                    "findings": [],
                }
            )

    def release_review_lock(
        self,
        status: str = "done",
        run_id: str | None = None,
        tokens: dict | None = None,
        error: str = "",
    ) -> None:
        with self._state_lock:
            self._review_state["running"] = False
            self._review_state["status"] = status
            self._review_state["finished_at"] = time.monotonic()
            self._review_state["finished_at_wall"] = time.time()
            self._review_state["error"] = error
            if run_id:
                self._review_state["run_id"] = run_id
            if tokens is not None:
                self._review_state["tokens"] = tokens
        self._review_lock.release()

    def stop_review(self) -> dict:
        """Ask the one running review to stop. Any tab may call this."""
        with self._state_lock:
            if not self._review_state["running"]:
                raise RuntimeError(t("server.no_active_review"))
            self._stop_event.set()
            if self._review_state["status"] != "stopping":
                self._review_state["status"] = "stopping"
                self._review_state["messages"].append(t("server.stop_requested_log"))
        return {"ok": True, "message": t("server.stop_requested")}

    def _set_status(self, status: str, message: str = "") -> None:
        with self._state_lock:
            self._review_state["status"] = status
            if message:
                self._review_state["messages"].append(message)

    def _wait_for_slot(self) -> None:
        """Block until the hub grants an execution slot (other projects may run).

        Queued projects show status ``queued`` so their tab says 排队中 instead of
        looking frozen. Stopping while queued aborts cleanly.
        """
        if self._gate is None:
            return
        self._set_status("queued", t("server.queued"))
        while not self._gate.acquire(timeout=0.25):
            if self._stop_event.is_set():
                raise ReviewCancelled(t("server.queue_cancelled"))
        self._set_status("running")

    def execute_prepared(self, params: dict, progress=None, on_findings=None) -> dict:
        def both(msg: str) -> None:
            self._note_progress(msg)
            if progress:
                progress(msg)

        # Announce a clamped limit before anything else: if the run then queues or
        # fails, the reason the budget is not what was asked for is still on record.
        for note in params.get("notices") or []:
            both(note)
        self._wait_for_slot()
        try:
            outcome = execute_review(
                self.repo_root,
                diff_spec=params["diff_spec"],
                mode=params["mode"],
                max_units=params["max_units"],
                models=params.get("models") or None,
                author_model=params["author"],
                out=params["out"],
                registry=self.registry,
                house_rules=params["house_rules"],
                paths=params["paths"],
                progress=both,
                should_stop=self._stop_event.is_set,
                on_findings=on_findings,
            )
        finally:
            if self._gate is not None:
                self._gate.release()
        summary = outcome.summary()
        if outcome.out_path:
            summary["out_path"] = str(outcome.out_path)
        if params.get("notices"):
            summary["notices"] = list(params["notices"])
        return summary

    def start_review(self, payload: dict, progress=None) -> dict:
        params = self.prepare_review(payload)
        self.acquire_review_lock(params)
        final_status, run_id, final_tokens, final_error = "done", None, None, ""
        try:
            summary = self.execute_prepared(params, progress=progress)
            run_id = summary.get("run_id")
            final_tokens = summary.get("tokens")
            return summary
        except NoChanges:
            final_status = "empty"
            raise
        except ReviewCancelled:
            final_status = "cancelled"
            return {"cancelled": True, "error": t("server.stopped_by_user")}
        except Exception as exc:
            final_status = "error"
            final_error = _safe_error(exc, self.repo_root)
            raise
        finally:
            self.release_review_lock(
                status=final_status,
                run_id=run_id,
                tokens=final_tokens,
                error=final_error,
            )

    def set_status(self, finding_id: str, status: str, reason: str = "") -> dict:
        if not FINDING_ID_RE.match(finding_id):
            raise LookupError("invalid finding id")
        if status not in ("open", "accepted", "rejected"):
            raise ValueError("status must be open/accepted/rejected")
        found = self.store.find_finding(finding_id)
        if not found:
            raise LookupError(f"no finding starts with {finding_id}")
        target = found[0]["id"]
        self.store.set_status(target, status, reason)
        return {"id": target, "status": status, "reason": reason}


def _split_zones(findings: list[dict], mode: str) -> dict:
    zones: dict[str, list[dict]] = {"unique": [], "conflict": [], "consensus": []}
    order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    for f in sorted(findings, key=lambda x: (order.get(x.get("severity"), 9), x.get("id", ""))):
        agreement = f.get("agreement", "unverified")
        if agreement == "conflict":
            zones["conflict"].append(f)
        elif agreement in ("unanimous", "majority"):
            zones["consensus"].append(f)
        else:
            zones["unique"].append(f)
    return zones


def make_handler(app: WebApp | None, hub=None) -> type[BaseHTTPRequestHandler]:
    """Build the request handler.

    ``hub`` is optional: without it this is the classic single-repo UI (``/`` is
    the review page and ``/api/*`` targets ``app``). With it, ``/`` becomes the
    project shell and every project lives under ``/p/<slug>/`` while the legacy
    root API keeps pointing at the first project for compatibility.

    ``app`` is optional only in hub mode, for a launcher that starts with no
    repository selected yet.
    """

    class Handler(BaseHTTPRequestHandler):
        server_version = "CodeErratumUI"

        # -- plumbing --------------------------------------------------

        def log_message(self, fmt: str, *args) -> None:  # quieter than the default
            return

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            # A tab that closed mid-request is not a server error: drop the
            # connection quietly instead of printing a traceback per response.
            try:
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                self.close_connection = True

        def _json(self, code: int, payload: dict) -> None:
            self._send(
                code,
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
            )

        def _error(self, code: int, message: str) -> None:
            self._json(code, {"error": message})

        def _sse_headers(self) -> None:
            """Start an event stream. No Content-Length: events follow until done."""
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store")
            # Finite one-shot stream: closing after the terminal frame lets the
            # browser's reader.resolve() instead of waiting forever on keep-alive.
            self.send_header("Connection", "close")
            self.close_connection = True
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()

        def _sse_event(self, kind: str, payload: dict) -> bool:
            """Write one SSE frame; flush so the browser sees progress immediately.

            False means the reader is gone — callers that emit many frames (one
            per finding) can stop early instead of paying for failed writes.
            """
            if self._client_gone:
                return False
            if not _write_sse(self.wfile, kind, payload):
                self._client_gone = True
                return False
            return True

        def _stream_findings(self, app: WebApp, _unit, findings, model_id: str) -> None:
            """Push each unit's findings as they are parsed, not at end of run.

            One frame per finding keeps the tab's list truthful while the run is
            still on: a 40-unit review takes minutes, and a log that only ever
            says "unit 12/53" gives no reason to believe anything is being found.

            Every brief is recorded on the app before it is sent, and recording
            continues after the client goes away — the run outlives the tab, and
            whoever picks it up through /api/review/status must still see them.
            """
            for finding in findings:
                brief = _finding_brief(finding, model_id)
                app._note_finding(brief)
                if not self._client_gone:
                    self._sse_event("finding", {"finding": brief})

        def _stream_review(self, app: WebApp, body: dict) -> None:
            """Run a review while pushing progress frames over one SSE response.

            Validation and lock contention are answered as ordinary JSON errors
            *before* the stream starts, so the browser still gets a real 400/409.
            Once the stream starts, failures travel as an ``error`` event. If the
            browser goes away mid-run we keep reviewing and stop writing frames.
            """
            self._client_gone = False
            try:
                params = app.prepare_review(body)
                app.acquire_review_lock(params)
            except RuntimeError as exc:  # lock contention
                self._error(HTTPStatus.CONFLICT, str(exc))
                return
            except (PipelineError, ValueError) as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            final_status, final_run_id, final_tokens, final_error = "done", None, None, ""
            try:
                try:
                    self._sse_headers()
                except OSError:  # tab closed before the stream opened
                    self._client_gone = True
                self._sse_event("log", {"message": t("server.stream_connected")})
                summary = app.execute_prepared(
                    params,
                    progress=lambda msg: self._sse_event("log", {"message": msg}),
                    on_findings=lambda unit, found, model: self._stream_findings(
                        app, unit, found, model
                    ),
                )
                final_run_id = summary.get("run_id")
                final_tokens = summary.get("tokens")
                self._sse_event("done", summary)
            except ReviewCancelled as exc:
                final_status = "cancelled"
                self._sse_event("cancelled", {"message": str(exc) or t("server.stopped_by_user")})
            except NoChanges as exc:
                final_status = "empty"
                self._sse_event("empty", {"message": str(exc)})
            except (PipelineError, ValueError) as exc:
                final_status = "error"
                final_error = _safe_error(exc, app.repo_root)
                self._sse_event("error", {"error": final_error})
            except Exception as exc:  # noqa: BLE001 - keep the stream alive, hide local paths
                final_status = "error"
                final_error = _safe_error(exc, app.repo_root)
                print(
                    f"[web] stream review: {type(exc).__name__}: {exc}\n{traceback.format_exc()}",
                    file=sys.stderr,
                )
                self._sse_event("error", {"error": final_error})
            finally:
                app.release_review_lock(
                    status=final_status,
                    run_id=final_run_id,
                    tokens=final_tokens,
                    error=final_error,
                )

        def _host_ok(self) -> bool:
            """Stop DNS rebinding: evil.com pointing at 127.0.0.1 is not a local user."""
            return bool(LOOPBACK_HOST_RE.match(self.headers.get("Host") or ""))

        def _origin_ok(self) -> bool:
            """Only same-origin, loopback POSTs may spend tokens or write state.

            A missing Origin used to mean "safe" — it does not. A plain HTML form
            can POST a text/plain body containing JSON without a preflight, and
            the body parser accepts it. Require Origin explicitly, and require
            it to match the loopback Host exactly.
            """
            host = self.headers.get("Host") or ""
            if not LOOPBACK_HOST_RE.match(host):
                return False
            origin = self.headers.get("Origin")
            if not origin:
                return False
            parts = urlsplit(origin)
            return parts.scheme in ("http", "https") and parts.netloc == host

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ValueError("request body too large")
            if not length:
                return {}
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("body must be a JSON object")
            return data

        # -- routing ---------------------------------------------------

        def _resolve_project(self, path: str):
            """Split ``/p/<slug>/...`` into (project app, inner path, slug).

            A non-hub server passes paths through untouched so single-repo
            behaviour stays byte-identical for existing tests and scripts.
            """
            if hub is None:
                return app, path, None
            m = re.fullmatch(r"/p/([0-9a-f]{6,12})(/.*)?", path)
            if not m:
                # Desktop/bare launch: the hub may legitimately hold zero
                # projects, and then there is no root app to answer /api/*.
                if app is None:
                    raise LookupError(t("server.no_project"))
                return app, path, None
            slug = m.group(1)
            inner = (m.group(2) or "/").rstrip("/") or "/"
            return hub.get(slug), inner, slug

        def _hub_post(self, path: str, body: dict) -> None:
            """Project registry writes: add, pick, find, remove, open folder."""
            if path == "/api/projects":
                self._json(HTTPStatus.OK, hub.add(str(body.get("path") or "")))
                return
            if path == "/api/projects/pick":
                self._json(HTTPStatus.OK, hub.add_picked())
                return
            if path == "/api/projects/find":
                hints = body.get("hints") or []
                if not isinstance(hints, list):
                    hints = []
                found = hub.first_or_raise().find_repo_by_name(str(body.get("name") or ""), hints)
                self._json(HTTPStatus.OK, found)
                return
            m = re.fullmatch(r"/api/projects/([0-9a-f]{6,12})/(remove|open)", path)
            if not m:
                raise LookupError(f"no route for {path}")
            slug, action = m.group(1), m.group(2)
            if action == "remove":
                self._json(HTTPStatus.OK, hub.remove(slug))
            else:
                self._json(HTTPStatus.OK, hub.get(slug).open_repo_folder())

        def _project_page(self, slug: str) -> bytes:
            html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
            # The page's JS prefixes every request with this base, so the same
            # file works at / (legacy) and under /p/<slug>/ inside a tab.
            return html.replace(
                '<html lang="zh-CN">',
                f'<html lang="zh-CN" data-base="/p/{slug}">',
                1,
            ).encode("utf-8")

        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            set_lang(self.headers.get("X-Lang"))  # per-thread: never leaks across tabs
            if not self._host_ok():
                self._error(HTTPStatus.FORBIDDEN, "non-loopback Host refused")
                return
            path = urlsplit(self.path).path.rstrip("/") or "/"
            try:
                if hub is not None and path in ("/", "/index.html"):
                    html = (STATIC_DIR / "projects.html").read_bytes()
                    self._send(HTTPStatus.OK, html, "text/html; charset=utf-8")
                    return
                if hub is not None and path == "/api/projects":
                    self._json(HTTPStatus.OK, hub.snapshot())
                    return
                app, path, slug = self._resolve_project(path)
                if slug is not None and path in ("/", "/index.html"):
                    self._send(HTTPStatus.OK, self._project_page(slug), "text/html; charset=utf-8")
                elif path in ("/", "/index.html"):
                    html = (STATIC_DIR / "index.html").read_bytes()
                    self._send(HTTPStatus.OK, html, "text/html; charset=utf-8")
                elif path == "/api/meta":
                    self._json(HTTPStatus.OK, app.meta())
                elif path == "/api/config":
                    self._json(HTTPStatus.OK, app.config_info())
                elif path == "/api/models":
                    self._json(HTTPStatus.OK, app.models())
                elif path == "/api/runs":
                    self._json(HTTPStatus.OK, app.list_runs())
                elif path == "/api/stats":
                    self._json(HTTPStatus.OK, app.stats())
                elif path == "/api/review/status":
                    self._json(HTTPStatus.OK, app.review_status())
                elif path == "/api/docs":
                    self._json(HTTPStatus.OK, app.docs_overview())
                elif m := re.fullmatch(r"/api/runs/([0-9a-f]+)", path):
                    self._json(HTTPStatus.OK, app.get_run(m.group(1)))
                elif m := re.fullmatch(r"/api/runs/([0-9a-f]+)/review\.md", path):
                    text = app.read_markdown(m.group(1))
                    self._send(
                        HTTPStatus.OK,
                        text.encode("utf-8"),
                        "text/markdown; charset=utf-8",
                    )
                else:
                    self._error(HTTPStatus.NOT_FOUND, f"no route for {path}")
            except LookupError as exc:
                self._error(HTTPStatus.NOT_FOUND, str(exc))
            except Exception as exc:  # noqa: BLE001 - never kill the server on one request
                # Exception text can carry absolute local paths. Keep it in the
                # server log; a page loaded in a browser must not be able to
                # enumerate the user's filesystem through error messages.
                print(f"[web] GET {path}: {type(exc).__name__}: {exc}", file=sys.stderr)
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal error; see server log")

        def do_POST(self) -> None:  # noqa: N802 - stdlib naming
            set_lang(self.headers.get("X-Lang"))
            path = urlsplit(self.path).path.rstrip("/") or "/"
            if not self._origin_ok():
                self._error(HTTPStatus.FORBIDDEN, "cross-origin request refused")
                return
            try:
                body = self._body()
                if path == "/api/open/homepage":
                    # Project-independent: works from the shell with no repo open.
                    self._json(HTTPStatus.OK, open_homepage())
                    return
                if hub is not None and path.startswith("/api/projects"):
                    self._hub_post(path, body)
                    return
                app, path, _slug = self._resolve_project(path)
                if path == "/api/review/stream":
                    self._stream_review(app, body)
                elif path == "/api/review/stop":
                    self._json(HTTPStatus.OK, app.stop_review())
                elif path == "/api/review":
                    self._json(HTTPStatus.OK, app.start_review(body))
                elif path == "/api/config":
                    self._json(HTTPStatus.OK, app.save_config(body))
                elif path == "/api/repo":
                    self._json(HTTPStatus.OK, app.switch_repo(str(body.get("path") or "")))
                elif path == "/api/pick-repo":
                    self._json(HTTPStatus.OK, app.pick_repo())
                elif path == "/api/repo/open":
                    self._json(HTTPStatus.OK, app.open_repo_folder())
                elif path == "/api/model/check":
                    self._json(HTTPStatus.OK, app.check_model(body))
                elif path == "/api/find-repo":
                    hints = body.get("hints") or []
                    if not isinstance(hints, list):
                        hints = []
                    self._json(
                        HTTPStatus.OK,
                        app.find_repo_by_name(str(body.get("name") or ""), hints),
                    )
                elif m := re.fullmatch(r"/api/findings/([0-9a-f]+)/status", path):
                    self._json(
                        HTTPStatus.OK,
                        app.set_status(
                            m.group(1),
                            str(body.get("status", "")),
                            str(body.get("reason", "")),
                        ),
                    )
                else:
                    self._error(HTTPStatus.NOT_FOUND, f"no route for {path}")
            except LookupError as exc:
                self._error(HTTPStatus.NOT_FOUND, str(exc))
            except RuntimeError as exc:  # review lock contention
                self._error(HTTPStatus.CONFLICT, str(exc))
            except NoChanges as exc:
                # Nothing to review is not a failure — report it without a scary status.
                self._error(HTTPStatus.OK, str(exc))
            except (PipelineError, ValueError) as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            except Exception as exc:  # noqa: BLE001
                print(f"[web] POST {path}: {type(exc).__name__}: {exc}", file=sys.stderr)
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal error; see server log")

    return Handler


def make_server(
    app: WebApp | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    hub=None,
) -> ThreadingHTTPServer:
    """Bind a loopback server. ``port=0`` lets the OS pick — callers read
    ``server_address[1]`` for the port that was actually granted.

    ``app`` may be None when a hub is given: the desktop launcher starts with an
    empty project list and lets the shell add repositories.
    """
    if app is None and hub is None:
        raise ValueError("make_server needs either an app or a hub")
    httpd = ThreadingHTTPServer((host, port), make_handler(app, hub))
    httpd.daemon_threads = True
    return httpd
