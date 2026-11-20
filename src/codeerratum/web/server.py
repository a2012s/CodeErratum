"""Local web UI — standard library only, bound to loopback.

No FastAPI, no bundler, no node_modules: `codeerratum ui` has to work on a
machine that has nothing installed but this package. ThreadingHTTPServer gives
us "page stays responsive while a review runs" for free; a lock keeps two
reviews from running at once (the same cost guard as `--max-units`).

Security posture: binds 127.0.0.1 by default, rejects cross-origin POSTs (a
malicious page must not be able to trigger a paid review through your browser),
and validates run ids before touching the filesystem.
"""

from __future__ import annotations

import json
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from ..config import Config
from ..models import ModelRegistry
from ..pipeline import (
    HOUSE_RULES_FILE,
    STATE_DIR,
    NoChanges,
    PipelineError,
    execute_review,
    resolve_house_rules,
    store_for,
)
from ..review import MAX_HOUSE_RULES_CHARS

STATIC_DIR = Path(__file__).parent / "static"
RUN_ID_RE = re.compile(r"^[0-9a-f]{6,32}$")
FINDING_ID_RE = re.compile(r"^[0-9a-f]{6,32}$")
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
        info: dict = {
            "repo": str(self.repo_root),
            "state_dir": str(self.repo_root / STATE_DIR),
            "configured": False,
            "mode": "quick",
            "models": [],
            "config_error": "",
            "house_rules": self.house_rules_info(),
        }
        try:
            config = Config.from_env()
        except ValueError as exc:
            info["config_error"] = str(exc)
            return info
        registry = self.registry or config.registry
        info["configured"] = True
        info["mode"] = config.mode
        info["models"] = [
            {"model_id": m.model_id, "family": m.resolved_family()} for m in registry.models
        ]
        return info

    def house_rules_info(self) -> dict:
        """Report the auto-discovered rules file so an empty textarea is not ambiguous."""
        return resolve_house_rules(None, self.repo_root).summary() | {"path": HOUSE_RULES_FILE}

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
            # Rules arrive as text, never as a path: the UI must not be able to read
            # arbitrary local files, even though it only listens on loopback.
            house_rules = str(payload.get("house_rules") or "")
            if len(house_rules) > MAX_HOUSE_RULES_CHARS * 2:
                raise PipelineError(
                    f"house_rules is too long ({len(house_rules)} chars, "
                    f"limit {MAX_HOUSE_RULES_CHARS * 2})"
                )
            out = (
                Path(str(payload.get("out") or "review.md"))
                if payload.get("write_markdown")
                else None
            )
            outcome = execute_review(
                self.repo_root,
                diff_spec=diff_spec,
                mode=mode,
                max_units=max_units,
                author_model=author,
                out=out,
                registry=self.registry,
                house_rules=house_rules,
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

        def _origin_ok(self) -> bool:
            """Reject cross-site POSTs so a random web page cannot spend your tokens."""
            origin = self.headers.get("Origin")
            if not origin:
                return True
            return urlsplit(origin).netloc == (self.headers.get("Host") or "")

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
            path = urlsplit(self.path).path.rstrip("/") or "/"
            try:
                if path in ("/", "/index.html"):
                    html = (STATIC_DIR / "index.html").read_bytes()
                    self._send(HTTPStatus.OK, html, "text/html; charset=utf-8")
                elif path == "/api/meta":
                    self._json(HTTPStatus.OK, app.meta())
                elif path == "/api/runs":
                    self._json(HTTPStatus.OK, app.list_runs())
                elif path == "/api/stats":
                    self._json(HTTPStatus.OK, app.stats())
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
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(exc).__name__}: {exc}")

        def do_POST(self) -> None:  # noqa: N802 - stdlib naming
            path = urlsplit(self.path).path.rstrip("/") or "/"
            if not self._origin_ok():
                self._error(HTTPStatus.FORBIDDEN, "cross-origin request refused")
                return
            try:
                body = self._body()
                if path == "/api/review":
                    self._json(HTTPStatus.OK, app.start_review(body))
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
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(exc).__name__}: {exc}")

    return Handler


def make_server(app: WebApp, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    httpd.daemon_threads = True
    return httpd
