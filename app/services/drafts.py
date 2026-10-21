"""
Draft review and approval workflow.

    AI draft saved to Gmail  ->  pending_review
    reviewer edits           ->  edited       (Gmail draft updated)
    reviewer approves        ->  approved     (still a Gmail draft)
    person sends in Gmail    ->  sent         (detected by sync_status)
    reviewer rejects         ->  rejected     (Gmail draft removed)
    Gmail save failed        ->  failed       (can be retried)

Nothing in this module can send an email.
"""

import logging
import threading
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.gmail.drafts import GmailDraftManager, gmail_draft_manager
from app.gmail.reader import EmailMessage
from app.services import activity
from app.services.errors import Conflict, NotFound, ServiceError
from app.services.gmail_accounts import gmail_accounts
from app.store.db import get_db, now_iso

logger = logging.getLogger(__name__)

REVIEWABLE = ("pending_review", "edited", "approved")
MAX_BODY_CHARS = 20000


def gmail_link(account_email: str, thread_id: str) -> str:
    base = "https://mail.google.com/mail/u/"
    base += f"?authuser={account_email}" if account_email else "0/"
    return f"{base}#all/{thread_id}" if thread_id else f"{base}#drafts"


class DraftService:

    def __init__(self):
        self._locks: Dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._locks_guard = threading.Lock()

    def _lock(self, draft_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks[draft_id]

    # --- queries ---------------------------------------------------------
    def _row(self, workspace_id: str, draft_id: str) -> Dict[str, Any]:
        row = get_db().fetchone("SELECT * FROM drafts WHERE id = ? AND workspace_id = ?", (draft_id, workspace_id))
        if not row:
            raise NotFound("Draft not found. It may have been removed.")
        return row

    def _present(self, row: Dict[str, Any], email: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        account = get_db().fetchone("SELECT email FROM email_accounts WHERE id = ?", (row["account_id"],)) if row.get("account_id") else None
        out = {k: v for k, v in row.items() if k not in ("gmail_draft_id", "gmail_message_id", "workspace_id")}
        out["needs_attention"] = bool(row["needs_attention"])
        out["is_edited"] = row["body"].strip() != (row["original_body"] or "").strip()
        out["account_email"] = account["email"] if account else ""
        out["account_connected"] = bool(account)
        out["in_gmail"] = bool(row.get("gmail_draft_id")) and row["status"] in REVIEWABLE
        out["gmail_url"] = gmail_link(out["account_email"], row.get("thread_id", "")) if account else ""
        if email is not None:
            out["email"] = email
        return out

    def list(self, workspace_id: str, status: str = "") -> List[Dict[str, Any]]:
        sql = (
            "SELECT d.*, e.subject AS email_subject, e.sender AS email_sender, e.received_at AS email_received_at "
            "FROM drafts d LEFT JOIN emails e ON e.id = d.email_id WHERE d.workspace_id = ?"
        )
        params: List[Any] = [workspace_id]
        if status == "awaiting_review":
            sql += " AND d.status IN ('pending_review', 'edited')"
        elif status:
            sql += " AND d.status = ?"
            params.append(status)
        sql += " ORDER BY d.created_at DESC LIMIT 500"
        rows = get_db().fetchall(sql, params)
        result = []
        for r in rows:
            item = self._present(r)
            item["body_preview"] = " ".join(r["body"].split())[:160]
            item.pop("body", None)
            item.pop("original_body", None)
            result.append(item)
        return result

    def get(self, workspace_id: str, draft_id: str) -> Dict[str, Any]:
        row = self._row(workspace_id, draft_id)
        email = get_db().fetchone(
            "SELECT id, sender, recipient, subject, body, received_at, category, status, account_email "
            "FROM emails WHERE id = ?", (row["email_id"],))
        return self._present(row, email)

    def stats(self, workspace_id: str) -> Dict[str, int]:
        rows = get_db().fetchall(
            "SELECT status, COUNT(*) AS n FROM drafts WHERE workspace_id = ? GROUP BY status", (workspace_id,))
        by = {r["status"]: r["n"] for r in rows}
        return {
            "total": sum(v for k, v in by.items() if k != "failed"),
            "pending_review": by.get("pending_review", 0) + by.get("edited", 0),
            "approved": by.get("approved", 0),
            "sent": by.get("sent", 0),
            "rejected": by.get("rejected", 0),
            "failed": by.get("failed", 0),
        }

    # --- Gmail helpers ---------------------------------------------------
    def _original(self, row: Dict[str, Any]) -> EmailMessage:
        email = get_db().fetchone("SELECT * FROM emails WHERE id = ?", (row["email_id"],))
        if not email:
            raise ServiceError("The original email for this draft no longer exists.")
        return EmailMessage(
            id=email["gmail_message_id"], threadId=email["thread_id"],
            messageId=email["message_id_header"], references=email["references_header"],
            sender=email["sender"], subject=email["subject"], body=email["body"],
        )

    def _service(self, row: Dict[str, Any]):
        if not row.get("account_id") or not get_db().fetchone(
                "SELECT id FROM email_accounts WHERE id = ?", (row["account_id"],)):
            raise ServiceError(
                "The email account for this draft is disconnected. Reconnect it to continue.",
                code="account_missing",
            )
        return gmail_accounts.get_service(row["account_id"])

    def _require_gmail_draft(self, service, row: Dict[str, Any]):
        if not row.get("gmail_draft_id"):
            raise ServiceError("This draft is not saved in Gmail.")
        try:
            exists = GmailDraftManager.get_draft(service, row["gmail_draft_id"])
        except Exception:
            logger.exception("Could not verify Gmail draft %s", row["gmail_draft_id"])
            raise ServiceError("Gmail could not be reached. Please try again.", code="gmail_error")
        if not exists:
            try:
                self._sync_one(service, row)
            except Exception:
                logger.exception("Could not determine what happened to draft %s", row["id"])
            raise Conflict(
                "This draft no longer exists in Gmail. It may have been sent or deleted there.",
                code="draft_missing",
            )

    def _write_body(self, service, row: Dict[str, Any], body: str) -> None:
        try:
            GmailDraftManager.update_draft(service, row["gmail_draft_id"], self._original(row), body)
        except ServiceError:
            raise
        except Exception:
            logger.exception("Updating Gmail draft %s failed", row["gmail_draft_id"])
            raise ServiceError("The draft could not be updated in Gmail. Please try again.", code="gmail_error")

    @staticmethod
    def _clean_body(body: Any) -> str:
        if not isinstance(body, str):
            raise ServiceError("The response text is missing.")
        body = body.replace("\r\n", "\n").strip()
        if not body:
            raise ServiceError("The response cannot be empty.")
        if len(body) > MAX_BODY_CHARS:
            raise ServiceError("The response is too long.")
        return body

    # --- commands --------------------------------------------------------
    def update_body(self, workspace_id: str, draft_id: str, body: Any) -> Dict[str, Any]:
        body = self._clean_body(body)
        with self._lock(draft_id):
            row = self._row(workspace_id, draft_id)
            if row["status"] not in REVIEWABLE:
                raise Conflict("Only drafts awaiting review can be edited.")
            if body == row["body"].strip():
                return self.get(workspace_id, draft_id)
            service = self._service(row)
            self._require_gmail_draft(service, row)
            self._write_body(service, row, body)
            get_db().update("drafts", draft_id, {
                "body": body, "status": "edited", "status_message": "", "updated_at": now_iso(),
            })
            activity.log(workspace_id, "draft_edited", f"Draft edited: {row['subject']}", "draft", draft_id)
        return self.get(workspace_id, draft_id)

    def approve(self, workspace_id: str, draft_id: str, body: Any = None) -> Dict[str, Any]:
        with self._lock(draft_id):
            row = self._row(workspace_id, draft_id)
            if row["status"] == "approved":
                return self.get(workspace_id, draft_id)
            if row["status"] not in ("pending_review", "edited"):
                raise Conflict("This draft can no longer be approved.")
            service = self._service(row)
            self._require_gmail_draft(service, row)
            updates: Dict[str, Any] = {"status": "approved", "status_message": "", "updated_at": now_iso()}
            if body is not None:
                body = self._clean_body(body)
                if body != row["body"].strip():
                    self._write_body(service, row, body)
                    updates["body"] = body
            get_db().update("drafts", draft_id, updates)
            activity.log(workspace_id, "draft_approved", f"Draft approved — ready to send from Gmail: {row['subject']}", "draft", draft_id)
        return self.get(workspace_id, draft_id)

    def reject(self, workspace_id: str, draft_id: str, reason: str = "") -> Dict[str, Any]:
        with self._lock(draft_id):
            row = self._row(workspace_id, draft_id)
            if row["status"] == "rejected":
                return self.get(workspace_id, draft_id)
            if row["status"] not in REVIEWABLE and row["status"] != "failed":
                raise Conflict("This draft can no longer be rejected.")
            removed_message = ""
            if row.get("gmail_draft_id") and row["status"] != "failed":
                try:
                    service = self._service(row)
                    GmailDraftManager.delete_draft(service, row["gmail_draft_id"])
                except ServiceError as e:
                    removed_message = f"Rejected, but the Gmail draft could not be removed: {e.message}"
                except Exception:
                    logger.exception("Could not delete Gmail draft %s", row["gmail_draft_id"])
                    raise ServiceError("The draft could not be removed from Gmail. Please try again.", code="gmail_error")
            reason = (reason or "").strip()[:500]
            get_db().update("drafts", draft_id, {
                "status": "rejected",
                "status_message": removed_message or (f"Reason: {reason}" if reason else ""),
                "updated_at": now_iso(),
            })
            activity.log(workspace_id, "draft_rejected", f"Draft rejected: {row['subject']}", "draft", draft_id)
        return self.get(workspace_id, draft_id)

    def discard(self, workspace_id: str, draft_id: str, note: str) -> None:
        """Removes an unapproved draft (used when an email is processed again)."""
        with self._lock(draft_id):
            row = self._row(workspace_id, draft_id)
            if row.get("gmail_draft_id") and row["status"] in ("pending_review", "edited"):
                try:
                    GmailDraftManager.delete_draft(self._service(row), row["gmail_draft_id"])
                except Exception:
                    logger.warning("Could not delete replaced Gmail draft %s", row["gmail_draft_id"])
            get_db().update("drafts", draft_id, {"status": "rejected", "status_message": note, "updated_at": now_iso()})

    def retry(self, workspace_id: str, draft_id: str) -> Dict[str, Any]:
        """Saves a draft whose Gmail creation failed."""
        with self._lock(draft_id):
            row = self._row(workspace_id, draft_id)
            if row["status"] != "failed":
                raise Conflict("Only drafts that failed to save can be retried.")
            service = self._service(row)
            created = gmail_draft_manager.create_draft_reply(self._original(row), row["body"], service=service)
            if not created or not created.get("id"):
                raise ServiceError("The draft could not be saved to Gmail. Please try again.", code="gmail_error")
            get_db().update("drafts", draft_id, {
                "gmail_draft_id": created["id"],
                "gmail_message_id": (created.get("message") or {}).get("id"),
                "status": "pending_review", "status_message": "", "updated_at": now_iso(),
            })
            get_db().update("emails", row["email_id"], {"status": "processed", "status_message": ""})
            activity.log(workspace_id, "draft_created", f"AI draft ready for review: {row['subject']}", "draft", draft_id)
        return self.get(workspace_id, draft_id)

    # --- Gmail status sync -------------------------------------------------
    def _sync_one(self, service, row: Dict[str, Any]) -> Optional[str]:
        """If the Gmail draft is gone, decide whether it was sent or deleted."""
        if GmailDraftManager.get_draft(service, row["gmail_draft_id"]):
            return None
        new_status, note = "rejected", "The draft was deleted in Gmail without being sent."
        if row.get("thread_id"):
            thread = service.users().threads().get(userId="me", id=row["thread_id"], format="minimal").execute()
            created_ms = datetime.fromisoformat(row["created_at"]).timestamp() * 1000
            for msg in thread.get("messages", []):
                if "SENT" in msg.get("labelIds", []) and int(msg.get("internalDate", "0")) >= created_ms - 60000:
                    new_status, note = "sent", ""
                    break
        get_db().update("drafts", row["id"], {"status": new_status, "status_message": note, "updated_at": now_iso()})
        verb = "Reply sent from Gmail" if new_status == "sent" else "Draft removed in Gmail"
        activity.log(row["workspace_id"], f"draft_{new_status}", f"{verb}: {row['subject']}", "draft", row["id"])
        return new_status

    def sync_status(self, workspace_id: str) -> Dict[str, int]:
        """Checks Gmail for drafts that were sent or deleted by a person."""
        rows = get_db().fetchall(
            "SELECT * FROM drafts WHERE workspace_id = ? AND status IN ('pending_review', 'edited', 'approved') "
            "AND gmail_draft_id IS NOT NULL", (workspace_id,))
        changed = {"sent": 0, "rejected": 0, "errors": 0}
        services: Dict[str, Any] = {}
        for row in rows:
            if row["account_id"] in services and services[row["account_id"]] is None:
                continue  # account already failed during this sync
            try:
                if row["account_id"] not in services:
                    services[row["account_id"]] = self._service(row)
                with self._lock(row["id"]):
                    fresh = get_db().fetchone("SELECT * FROM drafts WHERE id = ?", (row["id"],))
                    if not fresh or fresh["status"] not in REVIEWABLE:
                        continue
                    result = self._sync_one(services[row["account_id"]], fresh)
                if result:
                    changed[result] += 1
            except ServiceError:
                changed["errors"] += 1
                services[row["account_id"]] = None
            except Exception:
                logger.exception("Draft status sync failed for %s", row["id"])
                changed["errors"] += 1
        return changed


draft_service = DraftService()
