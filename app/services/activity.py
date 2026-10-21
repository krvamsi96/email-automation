"""Workspace activity log shown on the dashboard and in the notifications panel."""

from typing import Any, Dict, List

from app.store.db import get_db, now_iso


def log(workspace_id: str, kind: str, message: str, ref_type: str = "", ref_id: str = "") -> None:
    get_db().insert("activity", {
        "workspace_id": workspace_id,
        "kind": kind,
        "message": message,
        "ref_type": ref_type,
        "ref_id": ref_id,
        "created_at": now_iso(),
    })


def recent(workspace_id: str, limit: int = 20) -> List[Dict[str, Any]]:
    return get_db().fetchall(
        "SELECT id, kind, message, ref_type, ref_id, created_at FROM activity "
        "WHERE workspace_id = ? ORDER BY id DESC LIMIT ?",
        (workspace_id, max(1, min(limit, 100))),
    )
