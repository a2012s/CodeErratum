"""SQLite store — review archive + accept/reject feedback + stats.

Second-phase feature: the feedback loop that turns daily usage into both a
better product (suppressed false positives) and resume-ready evidence (stats).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ..models import FindingCollection


class ReviewStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._conn() as conn:
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

    def save_comparison(self, collection: FindingCollection, created_at: str) -> None:
        """Phase-3: persist cross-model comparison data for the eval/resume narrative."""
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
            # Update per-model profile from verdicts across all findings. Events are
            # keyed by (run, finding, model, role) so re-running the same review
            # cannot inflate the profile.
            for f in collection.findings:
                for v in f.verdicts:
                    col = {
                        "assert": "n_assert",
                        "refute": "n_refuted",
                        "confirm": "n_confirmed",
                    }.get(v.role)
                    if not col:
                        continue
                    if not self._record_event(conn, collection.run_id, f.id, v.model_id, v.role):
                        continue
                    conn.execute(
                        "INSERT INTO model_profile "
                        "(model_id, family, n_assert, n_unique, n_refuted, n_confirmed) "
                        "VALUES (?,?,0,0,0,0) ON CONFLICT(model_id) DO NOTHING",
                        (v.model_id, ""),
                    )
                    conn.execute(
                        f"UPDATE model_profile SET {col} = {col} + 1 WHERE model_id = ?",
                        (v.model_id,),
                    )
                if len(f.source_models) == 1 and f.agreement == "unique":
                    source = f.source_models[0]
                    if self._record_event(conn, collection.run_id, f.id, source, "unique"):
                        conn.execute(
                            "INSERT INTO model_profile "
                            "(model_id, family, n_assert, n_unique, n_refuted, n_confirmed) "
                            "VALUES (?,?,0,0,0,0) ON CONFLICT(model_id) DO NOTHING",
                            (source, ""),
                        )
                        conn.execute(
                            "UPDATE model_profile SET n_unique = n_unique + 1 WHERE model_id = ?",
                            (source,),
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
            by_model = conn.execute(
                "SELECT model_id, COUNT(*) AS n FROM runs GROUP BY model_id ORDER BY n DESC"
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
        }
