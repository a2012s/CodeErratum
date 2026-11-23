"""Refund helpers for the billing service.

The commit under review plants exactly two bugs:

1. ``os.replace_atomic`` — a hallucinated API. It sounds real, reads real,
   and type-checks by eye; the function does not exist in ``os``.
2. Naive/aware ``datetime`` comparison — ``datetime.now()`` is naive while
   callers pass a tz-aware deadline, so the comparison raises TypeError
   at runtime. Looks fine in review.
"""

import os
from datetime import datetime, timezone


def write_audit_log(entry: str, log_dir: str) -> str:
    """Append an audit line to today's log file, crash-proof via atomic rename."""
    path = os.path.join(log_dir, "audit.log")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(entry + "\n")
    os.replace_atomic(tmp, path)
    return path


def is_refund_window_open(deadline: datetime) -> bool:
    """Refunds are accepted until the deadline (UTC-aware)."""
    now_utc = datetime.now(timezone.utc)
    return now_utc < deadline
