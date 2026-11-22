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
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from ..config_store import KEEP_SECRET
from ..config_store import load as load_config
from ..config_store import public as public_config
from ..config_store import save as save_config_file
from ..ingest import DiffError, normalize_pathspec
from ..model_check import probe_model
from ..models import ModelRegistry
from ..pipeline import (
    HOUSE_RULES_FILE,
    STATE_DIR,
    NoChanges,
    PipelineError,
    execute_review,
    find_repo_root,
    resolve_house_rules,
    store_for,
)
from ..review import MAX_HOUSE_RULES_CHARS

STATIC_DIR = Path(__file__).parent / "static"
RUN_ID_RE = re.compile(r"^[0-9a-f]{6,32}$")
FINDING_ID_RE = re.compile(r"^[0-9a-f]{6,32}$")
LOOPBACK_HOST_RE = re.compile(r"^(?:127\.0\.0\.1|localhost|\[::1\]|::1)(?::\d+)?$", re.I)
MAX_BODY = 2 * 1024 * 1024


class WebApp:
    """Request-independent application logic, so tests can call it without sockets."""

    def __init__(self, repo_root: Path, registry: ModelRegistry | None = None) -> None:
        self.repo_root = repo_root.resolve()
        self.registry = registry
        self._review_lock = threading.Lock()

    @property
    def store(self):
        return store_for(self.repo_root)

    # ---- reads -----------------------------------------------------------

    def meta(self) -> dict:
        info = public_config(self.repo_root)
        info["repo"] = str(self.repo_root)
        info["state_dir"] = str(self.repo_root / STATE_DIR)
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
        if not candidate.exists():
            raise PipelineError(f"路径不存在：{candidate}")
        if not candidate.is_dir():
            raise PipelineError(f"不是文件夹：{candidate}")
        root = find_repo_root(candidate)
        if not (root / ".git").exists():
            raise PipelineError(f"不是 git 仓库：{root}")
        return root

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
            raise PipelineError("请填写本地文件夹路径")
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            raise PipelineError("请填写绝对路径（支持 ~ 开头的家目录）")
        root = self._git_root(candidate.resolve())
        if not self._review_lock.acquire(blocking=False):
            raise RuntimeError("审查进行中，不能切换仓库")
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
        system = sys.platform
        if system == "darwin":
            cmd = [
                "osascript",
                "-e",
                'POSIX path of (choose folder with prompt "选择要审查的 git 仓库")',
            ]
        elif system == "win32":
            cmd = [
                "powershell",
                "-NoProfile",
                "-Command",
                "Add-Type -AssemblyName System.Windows.Forms; "
                "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
                "$d.Description = '选择要审查的 git 仓库'; "
                "if ($d.ShowDialog() -eq 'OK') { $d.SelectedPath }",
            ]
        else:
            zenity = shutil.which("zenity")
            if not zenity:
                raise PipelineError("本系统未安装 zenity，无法弹出文件夹选择器；请粘贴路径")
            cmd = [zenity, "--file-selection", "--directory", "--title=选择要审查的 git 仓库"]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)
        except subprocess.TimeoutExpired as exc:
            raise PipelineError("文件夹选择超时，请重试或粘贴路径") from exc
        except OSError as exc:
            raise PipelineError(f"无法打开文件夹选择器：{exc}") from exc
        if result.returncode != 0 or not result.stdout.strip():
            # macOS -128 / "User canceled." — not an error worth a toast.
            raise PipelineError("已取消选择文件夹")
        chosen = result.stdout.strip().rstrip(os.sep) or os.sep
        root = self._git_root(Path(chosen).expanduser().resolve())
        return {"path": str(root), "repo": str(root)}

    def find_repo_by_name(self, raw_name: str, hints: list[str] | None = None) -> dict:
        """Resolve a dropped folder name to git repos on disk.

        A drag-and-drop `File`/`DirectoryEntry` exposes only the leaf name, never
        an absolute path. Search a bounded set of likely roots (recent repos,
        the current repo's neighbourhood, the home directory) so dropping a
        folder still lands on the right repository without a disk-wide walk.
        """
        name = str(raw_name or "").strip().strip("/\\")
        if not name or name in (".", "..") or "/" in name or "\\" in name:
            raise PipelineError("拖入的文件夹名无效")
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
                saved = load_config(self.repo_root)
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

    # ---- writes ----------------------------------------------------------

    def start_review(self, payload: dict) -> dict:
        if not self._review_lock.acquire(blocking=False):
            raise RuntimeError("a review is already running in this process")
        try:
            diff_spec = str(payload.get("diff") or "HEAD~1").strip() or "HEAD~1"
            mode = str(payload.get("mode") or "").strip().lower()
            try:
                max_units = int(payload.get("max_units", 10))
            except (TypeError, ValueError) as exc:
                raise PipelineError("max_units must be an integer") from exc
            max_units = max(1, min(max_units, 50))
            author = str(payload.get("author_model") or "").strip() or None
            # Validate here so a bad path is a 400 with a clear message, not a
            # git failure buried in a 500 after the review has started.
            try:
                paths = normalize_pathspec(str(payload.get("paths") or ""))
            except DiffError as exc:
                raise PipelineError(str(exc)) from exc
            # Rules arrive as text, never as a path: the UI must not be able to read
            # arbitrary local files, even though it only listens on loopback.
            house_rules = str(payload.get("house_rules") or "")
            if len(house_rules) > MAX_HOUSE_RULES_CHARS * 2:
                raise PipelineError(
                    f"house_rules is too long ({len(house_rules)} chars, "
                    f"limit {MAX_HOUSE_RULES_CHARS * 2})"
                )
            out: Path | None = None
            if payload.get("write_markdown"):
                raw_out = str(payload.get("out") or "review.md")
                # execute_review resolves this against the repo root. Without a
                # plain-filename check, "../../.ssh/authorized_keys" or an
                # absolute path would be a legal target for a local page to write.
                if "/" in raw_out or "\\" in raw_out or raw_out in (".", "..", ""):
                    raise PipelineError("out must be a plain filename in the repository root")
                out = Path(raw_out)
            outcome = execute_review(
                self.repo_root,
                diff_spec=diff_spec,
                mode=mode,
                max_units=max_units,
                author_model=author,
                out=out,
                registry=self.registry,
                house_rules=house_rules,
                paths=paths,
            )
            summary = outcome.summary()
            if outcome.out_path:
                summary["out_path"] = str(outcome.out_path)
            return summary
        finally:
            self._review_lock.release()

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


def make_handler(app: WebApp) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "CodeErratumUI"

        # -- plumbing --------------------------------------------------

        def log_message(self, fmt: str, *args) -> None:  # quieter than the default
            return

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, payload: dict) -> None:
            self._send(
                code,
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
            )

        def _error(self, code: int, message: str) -> None:
            self._json(code, {"error": message})

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

        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            if not self._host_ok():
                self._error(HTTPStatus.FORBIDDEN, "non-loopback Host refused")
                return
            path = urlsplit(self.path).path.rstrip("/") or "/"
            try:
                if path in ("/", "/index.html"):
                    html = (STATIC_DIR / "index.html").read_bytes()
                    self._send(HTTPStatus.OK, html, "text/html; charset=utf-8")
                elif path == "/api/meta":
                    self._json(HTTPStatus.OK, app.meta())
                elif path == "/api/config":
                    self._json(HTTPStatus.OK, app.config_info())
                elif path == "/api/runs":
                    self._json(HTTPStatus.OK, app.list_runs())
                elif path == "/api/stats":
                    self._json(HTTPStatus.OK, app.stats())
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
            path = urlsplit(self.path).path.rstrip("/") or "/"
            if not self._origin_ok():
                self._error(HTTPStatus.FORBIDDEN, "cross-origin request refused")
                return
            try:
                body = self._body()
                if path == "/api/review":
                    self._json(HTTPStatus.OK, app.start_review(body))
                elif path == "/api/config":
                    self._json(HTTPStatus.OK, app.save_config(body))
                elif path == "/api/repo":
                    self._json(HTTPStatus.OK, app.switch_repo(str(body.get("path") or "")))
                elif path == "/api/pick-repo":
                    self._json(HTTPStatus.OK, app.pick_repo())
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


def make_server(app: WebApp, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    httpd.daemon_threads = True
    return httpd
