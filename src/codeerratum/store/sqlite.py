"""SQLite store — review archive + accept/reject feedback + stats.

Second-phase feature: the feedback loop that turns daily usage into both a
better product (suppressed false positives) and resume-ready evidence (stats).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ..models import FindingCollection


class ReviewStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        """One short-lived, explicitly closed connection per operation.

        ``with sqlite3.connect(...)`` only commits; it never closes. With SSE
        polling and status writes on separate threads those connections piled up
        until the database reported "locked" or ran out of descriptors. Busy
        timeout plus WAL lets readers and the occasional writer coexist.
        """
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init(self) -> None:
        with self._conn() as conn:
            # WAL keeps status polling from blocking an accept/reject write.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    commit_sha TEXT,
                    model_id TEXT,
                    created_at TEXT,
                    n_findings INTEGER,
                    n_rejected INTEGER
                );
                CREATE TABLE IF NOT EXISTS findings (
                    id TEXT PRIMARY KEY,
                    run_id TEXT,
                    commit_sha TEXT,
                    severity TEXT,
                    claim TEXT,
                    file TEXT,
                    category TEXT,
                    status TEXT DEFAULT 'open',
                    rejection_reason TEXT,
                    created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS comparisons (
                    run_id TEXT PRIMARY KEY,
                    mode TEXT,
                    raw_counts TEXT,
                    after_clustering INTEGER,
                    after_falsification INTEGER,
                    zones TEXT,
                    created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS model_profile (
                    model_id TEXT PRIMARY KEY,
                    family TEXT,
                    n_assert INTEGER DEFAULT 0,
                    n_unique INTEGER DEFAULT 0,
                    n_refuted INTEGER DEFAULT 0,
                    n_confirmed INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS verdict_events (
                    run_id TEXT,
                    finding_id TEXT,
                    model_id TEXT,
                    role TEXT,
                    PRIMARY KEY (run_id, finding_id, model_id, role)
                );
                CREATE TABLE IF NOT EXISTS run_rejections (
                    run_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    count INTEGER NOT NULL,
                    PRIMARY KEY (run_id, reason, model_id)
                );
                CREATE INDEX IF NOT EXISTS idx_findings_status ON findings(status);
                CREATE INDEX IF NOT EXISTS idx_findings_category ON findings(category);
                """
            )

    def save_run(self, collection: FindingCollection, created_at: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?)",
                (
                    collection.run_id,
                    collection.commit,
                    collection.model_id,
                    created_at,
                    len(collection.findings),
                    len(collection.rejected),
                ),
            )
            for f in collection.findings:
                # Re-running a review must not erase a human verdict. A plain
                # INSERT OR REPLACE reset status to 'open' and dropped the
                # acceptance rate — the exact number this product is built to prove.
                conn.execute(
                    "INSERT INTO findings "
                    "(id, run_id, commit_sha, severity, claim, file, category, status, created_at) "
                    "VALUES (?,?,?,?,?,?,?,'open',?) "
                    "ON CONFLICT(id) DO UPDATE SET "
                    "run_id=excluded.run_id, commit_sha=excluded.commit_sha, "
                    "severity=excluded.severity, claim=excluded.claim, "
                    "file=excluded.file, category=excluded.category",
                    (
                        f.id,
                        collection.run_id,
                        collection.commit,
                        f.severity,
                        f.claim,
                        f.anchors[0].file if f.anchors else "",
                        f.category.value,
                        created_at,
                    ),
                )
            # Machine drops (anchor mismatch, provider error, refuted, ...)
            # are not human rejections. A single n_rejected count hid which
            # failure mode dominated, which is the first thing to look at when
            # finding quality regresses.
            drops: dict[tuple[str, str], int] = {}
            for row in collection.rejected:
                key = (str(row.get("reason") or "unknown"), str(row.get("model") or ""))
                drops[key] = drops.get(key, 0) + 1
            for (reason, model), count in drops.items():
                conn.execute(
                    "INSERT OR REPLACE INTO run_rejections (run_id, reason, model_id, count) "
                    "VALUES (?,?,?,?)",
                    (collection.run_id, reason, model, count),
                )
        self.update_profiles(collection)

    def save_comparison(self, collection: FindingCollection, created_at: str) -> None:
        """Phase-3: persist cross-model comparison data for the eval/resume narrative.

        Profile accumulation deliberately does NOT happen here. It used to, next to
        ``update_profiles``, and the two copies counted every assertion twice while
        crediting a falsifier's ``confirm`` to the falsifier — the exact model that
        must never look accurate. ``model_profile`` has one writer now.
        """
        if not collection.comparison:
            return
        cmp = collection.comparison
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO comparisons VALUES (?,?,?,?,?,?,?)",
                (
                    collection.run_id,
                    cmp.get("mode", collection.mode),
                    json.dumps(cmp.get("raw_counts", {})),
                    cmp.get("after_clustering", 0),
                    cmp.get("after_falsification", 0),
                    json.dumps(cmp.get("zones", {})),
                    created_at,
                ),
            )

    @staticmethod
    def _record_event(
        conn: sqlite3.Connection, run_id: str, finding_id: str, model_id: str, role: str
    ) -> bool:
        """Insert a profile event; returns False when it was already counted."""
        cur = conn.execute(
            "INSERT OR IGNORE INTO verdict_events (run_id, finding_id, model_id, role) "
            "VALUES (?,?,?,?)",
            (run_id, finding_id, model_id, role),
        )
        return cur.rowcount > 0

    def comparison_history(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM comparisons ORDER BY created_at DESC LIMIT 50"
            ).fetchall()
            return [dict(r) for r in rows]

    def model_profiles(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM model_profile ORDER BY n_assert DESC").fetchall()
            return [dict(r) for r in rows]

    def update_profiles(self, collection: FindingCollection) -> None:
        """Accumulate the per-model record that later feeds routing.

        Assertions and uniqueness come from findings. Precision comes from the
        falsification verdicts rather than human adjudication, because verdicts
        accumulate on every run while accept/reject depends on someone remembering
        to click — and a routing signal that needs manual input never gets enough
        samples to clear the n>=30 threshold.

        A verdict is credited to the models that ASSERTED the finding, not to the
        falsifier who rendered it. A dedicated falsifier never submits findings;
        crediting it would give a perfect "precision" to a model that should not
        be seated as a reviewer, while the actual authors keep an empty profile.

        This is the only writer of ``model_profile``. Each increment is guarded by
        a ``verdict_events`` row keyed on (run, finding, model, role), so saving the
        same collection twice (CLI then UI, a retried write) cannot inflate the
        counts that routing depends on.
        """
        with self._conn() as conn:
            # Refuted findings are profile samples too. A model that claims a
            # unique bug and gets falsified must pay the n_refuted price — if
            # only survivors counted, precision would reward reporting volume.
            # They are never written to `findings`, so no human verdict is
            # created for a claim the falsifier already broke.
            for f in [*collection.findings, *collection.refuted_findings]:
                unique = f.agreement == "unique" and len(f.source_models) == 1
                # One finding is one precision sample for each asserting model, no
                # matter how many falsifiers weighed in. Refute wins ties so a
                # disputed finding never inflates its author's precision.
                roles = {v.role for v in f.verdicts}
                outcome = "refute" if "refute" in roles else "confirm" if "confirm" in roles else ""
                for model_id in f.source_models:
                    if not model_id:
                        continue
                    if self._record_event(conn, collection.run_id, f.id, model_id, "assert"):
                        self._bump_profile(conn, model_id, "n_assert")
                    if unique and self._record_event(
                        conn, collection.run_id, f.id, model_id, "unique"
                    ):
                        self._bump_profile(conn, model_id, "n_unique")
                    if outcome and self._record_event(
                        conn, collection.run_id, f.id, model_id, outcome
                    ):
                        column = "n_confirmed" if outcome == "confirm" else "n_refuted"
                        self._bump_profile(conn, model_id, column)

    @staticmethod
    def _bump_profile(conn: sqlite3.Connection, model_id: str, column: str) -> None:
        """Increment one profile column, creating the row on first sight.

        ``column`` is a code literal chosen by the caller, never user input — the
        table shape is fixed and SQL injection has no way in here.
        """
        conn.execute(
            "INSERT INTO model_profile (model_id, family) VALUES (?, '') "
            "ON CONFLICT(model_id) DO NOTHING",
            (model_id,),
        )
        conn.execute(
            f"UPDATE model_profile SET {column} = {column} + 1 WHERE model_id = ?",
            (model_id,),
        )

    def model_profile_map(self) -> dict[str, dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM model_profile").fetchall()
        return {
            r["model_id"]: {
                "family": r["family"] or "",
                "n_assert": r["n_assert"] or 0,
                "n_unique": r["n_unique"] or 0,
                "n_confirmed": r["n_confirmed"] or 0,
                "n_refuted": r["n_refuted"] or 0,
            }
            for r in rows
        }

    def rejected_category_counts(self) -> dict[str, int]:
        """Human-confirmed false positives per category.

        This is the only signal that says "this category has cried wolf before",
        and it is fed into ``rank_findings`` so a category the owner keeps rejecting
        drops below fresher findings instead of leading the work order every time.
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT category, COUNT(*) AS n FROM findings "
                "WHERE status = 'rejected' AND category IS NOT NULL GROUP BY category"
            ).fetchall()
        return {r["category"]: r["n"] for r in rows if r["category"]}

    def set_status(self, finding_id: str, status: str, reason: str = "") -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE findings SET status = ?, rejection_reason = ? WHERE id = ?",
                (status, reason, finding_id),
            )
            return cur.rowcount > 0

    def find_finding(self, prefix: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM findings WHERE id LIKE ? ORDER BY created_at DESC LIMIT 10",
                (prefix + "%",),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_runs(self, limit: int = 50) -> list[dict]:
        """Newest first, for the UI's run history."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT run_id, commit_sha, model_id, created_at, n_findings, n_rejected "
                "FROM runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def statuses_for(self, finding_ids: list[str]) -> dict[str, dict]:
        """Human verdicts keyed by finding id.

        Looked up by id and not by run_id on purpose: a finding id is a content
        hash, so the same defect resurfacing in a later run keeps its verdict,
        while the findings row's run_id points at whichever run saw it last.
        """
        if not finding_ids:
            return {}
        out: dict[str, dict] = {}
        with self._conn() as conn:
            for chunk_start in range(0, len(finding_ids), 500):
                chunk = finding_ids[chunk_start : chunk_start + 500]
                placeholders = ",".join("?" * len(chunk))
                rows = conn.execute(
                    "SELECT id, status, rejection_reason FROM findings "
                    f"WHERE id IN ({placeholders})",
                    chunk,
                ).fetchall()
                for r in rows:
                    out[r["id"]] = {"status": r["status"], "reason": r["rejection_reason"] or ""}
        return out

    def stats(self) -> dict:
        with self._conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0]
            accepted = conn.execute(
                "SELECT COUNT(*) FROM findings WHERE status='accepted'"
            ).fetchone()[0]
            rejected = conn.execute(
                "SELECT COUNT(*) FROM findings WHERE status='rejected'"
            ).fetchone()[0]
            by_category = conn.execute(
                "SELECT category, "
                "COUNT(*) AS total, "
                "SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END) AS rejected "
                "FROM findings GROUP BY category ORDER BY total DESC"
            ).fetchall()
            # `runs.model_id` is a comma-joined string in dual/committee mode,
            # so grouping it counted *combinations*, not models. Profile events
            # are already one row per model per role, keyed for idempotency.
            by_model = conn.execute(
                "SELECT model_id, COUNT(*) AS n FROM verdict_events "
                "WHERE role='assert' GROUP BY model_id ORDER BY n DESC"
            ).fetchall()
            by_model_roles = conn.execute(
                "SELECT model_id, role, COUNT(*) AS n FROM verdict_events "
                "GROUP BY model_id, role ORDER BY model_id, role"
            ).fetchall()
            dropped_by_reason = conn.execute(
                "SELECT reason, SUM(count) AS n FROM run_rejections "
                "GROUP BY reason ORDER BY n DESC"
            ).fetchall()
            n_runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            decided = accepted + rejected
        return {
            "runs": n_runs,
            "findings_total": total,
            "accepted": accepted,
            "rejected": rejected,
            "acceptance_rate": round(accepted / decided, 3) if decided else None,
            "by_category": [dict(r) for r in by_category],
            "by_model": [dict(r) for r in by_model],
            "by_model_roles": [dict(r) for r in by_model_roles],
            # Distinct from `rejected` above, which is the human verdict count.
            "dropped_by_reason": [dict(r) for r in dropped_by_reason],
            "dropped_total": sum(int(r["n"]) for r in dropped_by_reason),
        }
