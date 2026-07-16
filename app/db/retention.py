"""
app/db/retention.py
───────────────────
Data-retention purge: permanently delete detection events (and, via FK
cascades, their bounding boxes and notifications) older than N days.

Scope
─────
Purged:  detection_event rows — these carry the screenshots (frame_blob)
         and per-student activity, i.e. the personal data the Ethics
         section promises to keep "only as long as necessary".
         detection and notification rows cascade away with their event.
Kept:    monitoring_run rows (session metadata for the Reports page) and
         audit_log rows (the audit trail is deliberately immutable).

Deletes run in small batches so the SQLite write lock is never held long
enough to stall the monitoring hot path. Freed pages are reused by SQLite
for new frames rather than shrinking the file — run VACUUM manually if you
need the .db file itself to shrink after a large purge.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.db._core import _conn


def purge_old_events(days: int, batch_size: int = 500) -> int:
    """
    Delete every detection_event older than `days` days. Returns the number
    of events removed. days <= 0 means retention is disabled — no-op.
    Writes one audit_log entry when anything was actually deleted.
    """
    if days <= 0:
        return 0
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    c = _conn()
    total = 0
    while True:
        cur = c.execute(
            "DELETE FROM detection_event WHERE id IN ("
            "  SELECT id FROM detection_event WHERE detected_at < ? LIMIT ?"
            ")",
            (cutoff, batch_size),
        )
        c.commit()
        total += cur.rowcount
        if cur.rowcount < batch_size:
            break
    if total:
        from app.db.audit import _insert_audit
        _insert_audit(
            c, "system.retention_purge",
            detail=f"deleted {total} event(s) older than {days} day(s)",
        )
        c.commit()
    return total
