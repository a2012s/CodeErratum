"""Refund helpers for the billing service."""

import os
from datetime import datetime


def write_audit_log(entry: str, log_dir: str) -> str:
    """Append an audit line to today's log file."""
    path = os.path.join(log_dir, "audit.log")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(entry + "\n")
    return path


def is_refund_window_open(deadline: datetime) -> bool:
    """Refunds are accepted until the deadline."""
    return datetime.now() < deadline
