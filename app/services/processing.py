"""
Email processing service.

Checks connected inboxes for new unanswered emails, stores them, and runs each
one through the AI workflow. The workflow only ever creates Gmail drafts; every
draft waits for a person to review, approve and send it from Gmail.
"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

from app.config import Config
from app.graph.state import build_initial_state
from app.gmail.reader import GmailReader
from app.services import activity
from app.services.errors import Conflict, NotFound, ServiceError
from app.services.gmail_accounts import gmail_accounts
from app.services.workspaces import workspace_service
from app.store.db import dumps, get_db, loads, new_id, now_iso

logger = logging.getLogger(__name__)

EMAIL_LIST_COLUMNS = (
    "e.id, e.account_email, e.sender, e.recipient, e.subject, e.snippet, e.received_at, e.category, "
    "e.status, e.status_message, e.security_flag, e.processed_at"
)


class ProcessingService:

    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="processing")
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._busy_emails: set = set()

    # --- graph ----------------------------------------------------------
    @staticmethod
    def _graph():
        from app.graph.workflow import app as copilot_graph
        return copilot_graph

    # --- jobs -----------------------------------------------------------
    def current_job(self, workspace_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            job = self._jobs.get(workspace_id)
            return dict(job) if job else None

    def is_running(self, workspace_id: str) -> bool:
        job = self.current_job(workspace_id)
        return bool(job and job["status"] == "running")

    def start_check(self, workspace_id: str, trigger: str = "manual") -> Dict[str, Any]:
        with self._lock:
            job = self._jobs.get(workspace_id)
            if job and job["status"] == "running":
                raise Conflict("Emails are already being checked. Please wait for it to finish.", code="job_running")
            job = {
                "id": new_id(),
                "status": "running",
                "stage": "Checking for new emails…",
                "trigger": trigger,
                "started_at": now_iso(),
                "finished_at": None,
                "found": 0,
                "processed": 0,
                "drafts_created": 0,
                "errors": [],
                "message": "",
            }
            self._jobs[workspace_id] = job
        self._executor.submit(self._run_check_safe, workspace_id, job)
        return dict(job)

    def _update_job(self, job: Dict[str, Any], **values):
        with self._lock:
            job.update(values)

    def _run_check_safe(self, workspace_id: str, job: Dict[str, Any]):
        try:
            self._run_check(workspace_id, job)
        except Exception:
            logger.exception("Email check failed for workspace %s", workspace_id)
            self._update_job(job, status="failed", message="Checking emails failed. Please try again.")
        finally:
            with self._lock:
                if job["status"] == "running":
                    job["status"] = "completed"
                job["finished_at"] = now_iso()
                job["stage"] = ""

    def _run_check(self, workspace_id: str, job: Dict[str, Any]):
        settings = workspace_service.settings(workspace_id)["email"]
        accounts = [a for a in gmail_accounts.list(workspace_id) if a["status"] != "disconnected"]
        if not accounts:
            self._update_job(job, status="failed", message="No connected email account. Connect Gmail to start.")
            return

        new_ids: List[str] = []
        errors: List[str] = []
        for account in accounts:
            try:
                service = gmail_accounts.get_service(account["id"])
                exclude = {r["gmail_message_id"] for r in get_db().fetchall(
                    "SELECT gmail_message_id FROM emails WHERE workspace_id = ?", (workspace_id,))}
                reader = GmailReader(service=service)
                fetched = reader.fetch_unanswered_emails(
                    max_results=settings["max_per_check"],
                    account_email=account["email"],
                    lookback_days=settings["lookback_days"],
                    exclude_ids=exclude,
                    raise_errors=True,
                )
                for msg in fetched:
                    email_id = self._store_email(workspace_id, account, msg)
                    if email_id:
                        new_ids.append(email_id)
                get_db().update("email_accounts", account["id"], {"last_checked_at": now_iso()})
            except ServiceError as e:
                errors.append(f"{account['email']}: {e.message}")
            except Exception:
                logger.exception("Could not read inbox for %s", account["email"])
                gmail_accounts._set_status(account["id"], "error", "Gmail could not be read. Try reconnecting.")
                errors.append(f"{account['email']}: Gmail could not be read. Please reconnect your account.")

        self._update_job(job, found=len(new_ids), errors=errors)
        if new_ids:
            activity.log(workspace_id, "emails_received", f"{len(new_ids)} new email(s) received")

        if new_ids:
            # Each email waits mostly on the AI service, so several are answered at once.
            self._update_job(job, stage=f"Generating responses (0 of {len(new_ids)} done)…")
            with ThreadPoolExecutor(max_workers=min(Config.EMAIL_CONCURRENCY, len(new_ids)),
                                    thread_name_prefix="email-ai") as pool:
                futures = {pool.submit(self.process_email, workspace_id, email_id): email_id for email_id in new_ids}
                for done, future in enumerate(as_completed(futures), start=1):
                    try:
                        result = future.result()
                        with self._lock:
                            job["processed"] += 1
                            if result.get("draft_created"):
                                job["drafts_created"] += 1
                    except Exception:
                        logger.exception("Processing failed for email %s", futures[future])
                    self._update_job(job, stage=f"Generating responses ({done} of {len(new_ids)} done)…")

        self._update_job(job, stage="Updating draft status…")
        try:
            from app.services.drafts import draft_service
            draft_service.sync_status(workspace_id)
        except Exception:
            logger.exception("Draft status sync failed")

        if errors and not new_ids:
            self._update_job(job, status="failed", message=errors[0])
        elif not new_ids:
            self._update_job(job, message="No new emails.")
        else:
            self._update_job(job, message=f"{len(new_ids)} new email(s) processed.")

    def _store_email(self, workspace_id: str, account: Dict[str, Any], msg) -> Optional[str]:
        email_id = new_id()
        extra = {"labels": msg.labels, "list_unsubscribe": msg.list_unsubscribe, "auto_submitted": msg.auto_submitted}
        try:
            get_db().insert("emails", {
                "id": email_id,
                "workspace_id": workspace_id,
                "account_id": account["id"],
                "account_email": account["email"],
                "gmail_message_id": msg.id,
                "thread_id": msg.threadId,
                "message_id_header": msg.messageId,
                "references_header": msg.references,
                "sender": msg.sender,
                "recipient": msg.recipient or account["email"],
                "subject": msg.subject,
                "body": msg.body,
                "snippet": msg.snippet,
                "received_at": msg.date or now_iso(),
                "status": "new",
                "knowledge_sources": dumps([]),
                "meta": dumps(extra),
                "created_at": now_iso(),
            })
        except Exception:
            # Already stored (unique per workspace + Gmail message id)
            return None
        return email_id

    # --- single email ----------------------------------------------------
    def process_email(self, workspace_id: str, email_id: str) -> Dict[str, Any]:
        with self._lock:
            if email_id in self._busy_emails:
                raise Conflict("This email is already being processed.")
            self._busy_emails.add(email_id)
        try:
            return self._process_email(workspace_id, email_id)
        finally:
            with self._lock:
                self._busy_emails.discard(email_id)

    def is_busy(self, email_id: str) -> bool:
        with self._lock:
            return email_id in self._busy_emails

    def _process_email(self, workspace_id: str, email_id: str) -> Dict[str, Any]:
        db = get_db()
        email = db.fetchone("SELECT * FROM emails WHERE id = ? AND workspace_id = ?", (email_id, workspace_id))
        if not email:
            raise NotFound("Email not found.")
        extra = loads(email.get("meta"), {}) or {}
        db.update("emails", email_id, {"status": "processing", "status_message": ""})

        settings = workspace_service.settings(workspace_id)
        state = build_initial_state()
        state.update({
            "emails_queue": [{
                "id": email["gmail_message_id"],
                "threadId": email["thread_id"],
                "messageId": email["message_id_header"],
                "references": email["references_header"],
                "sender": email["sender"],
                "subject": email["subject"],
                "body": email["body"],
                "snippet": email["snippet"],
                "labels": extra.get("labels", []),
                "list_unsubscribe": extra.get("list_unsubscribe", ""),
                "auto_submitted": extra.get("auto_submitted", ""),
            }],
            "workspace_id": workspace_id,
            "account_id": email["account_id"] or "",
            "company_name": settings["branding"]["company_name"],
            "tone": settings["ai"]["tone"],
            "signature": settings["ai"]["signature"],
            "draft_when_unknown": settings["ai"]["draft_when_unknown"],
            "skip_promotions": settings["email"]["skip_promotions"],
            "min_relevance": settings["advanced"]["min_relevance"],
        })
        if not email["account_id"]:
            db.update("emails", email_id, {
                "status": "failed",
                "status_message": "The email account for this message is no longer connected.",
            })
            return {"email_id": email_id, "draft_created": False}

        try:
            final = self._graph().invoke(state, {"recursion_limit": 60})
        except Exception:
            logger.exception("AI workflow failed for email %s", email_id)
            db.update("emails", email_id, {
                "status": "needs_attention",
                "status_message": "Unable to generate a response. The email has been marked for human review.",
                "processed_at": now_iso(),
            })
            activity.log(workspace_id, "processing_failed", f"Needs human review: {email['subject']}", "email", email_id)
            return {"email_id": email_id, "draft_created": False}

        return self._record(workspace_id, email, final)

    def _record(self, workspace_id: str, email: Dict[str, Any], final: Dict[str, Any]) -> Dict[str, Any]:
        db = get_db()
        email_id = email["id"]
        category = final.get("email_category", "")
        sources = final.get("knowledge_sources") or []
        base = {
            "processed_at": now_iso(), "knowledge_sources": dumps(sources), "category": category,
            "question": final.get("primary_question", "") or "",
            "evidence": dumps(final.get("knowledge_evidence") or []),
        }

        if final.get("is_suspicious"):
            db.update("emails", email_id, {
                **base,
                "category": "security",
                "status": "flagged",
                "security_flag": final.get("threat_type", "suspicious"),
                "security_reason": final.get("threat_reason", ""),
                "status_message": "No reply was drafted because this email looks suspicious.",
            })
            activity.log(workspace_id, "email_flagged", f"Security review needed: {email['subject']}", "email", email_id)
            return {"email_id": email_id, "draft_created": False}

        if category == "unrelated":
            db.update("emails", email_id, {
                **base, "status": "skipped",
                "status_message": "Promotional or unrelated email. No reply needed.",
            })
            return {"email_id": email_id, "draft_created": False}

        if final.get("draft_skipped_reason") == "no_knowledge":
            db.update("emails", email_id, {
                **base, "status": "needs_attention",
                "status_message": "No matching information was found in the knowledge base. Please reply personally.",
            })
            activity.log(workspace_id, "needs_attention", f"Needs a personal reply: {email['subject']}", "email", email_id)
            return {"email_id": email_id, "draft_created": False}

        body = final.get("generated_email", "")
        attention = ""
        if final.get("ai_error"):
            attention = final["ai_error"]
            if attention.startswith("Unable to generate"):
                attention = "Unable to generate a response, so a holding reply was drafted. Please write the answer before approving."
        elif final.get("knowledge_answer") == "INSUFFICIENT_KNOWLEDGE":
            attention = "No matching information was found in the knowledge base, so an acknowledgement was drafted."

        from app.gmail.drafts import reply_subject
        draft_id = new_id()
        created = bool(final.get("draft_id"))
        db.insert("drafts", {
            "id": draft_id,
            "workspace_id": workspace_id,
            "email_id": email_id,
            "account_id": email["account_id"],
            "gmail_draft_id": final.get("draft_id") or None,
            "gmail_message_id": final.get("draft_message_id") or None,
            "thread_id": email["thread_id"],
            "recipient": email["sender"],
            "subject": reply_subject(email["subject"]),
            "body": body,
            "original_body": body,
            "status": "pending_review" if created else "failed",
            "status_message": "" if created else (final.get("draft_error") or "The draft could not be saved to Gmail."),
            "needs_attention": 1 if attention else 0,
            "attention_reason": attention,
            "created_at": now_iso(),
            "updated_at": now_iso(),
        })
        generation_failed = (final.get("ai_error") or "").startswith("Unable to generate")
        if not created:
            email_status, email_message = "failed", "The AI response was generated but could not be saved to Gmail."
        elif generation_failed:
            email_status = "needs_attention"
            email_message = ("Unable to generate a response. The email has been marked for human review; "
                             "a holding reply was saved as a draft.")
        else:
            email_status, email_message = "processed", ""
        db.update("emails", email_id, {**base, "status": email_status, "status_message": email_message})
        if created:
            activity.log(workspace_id, "draft_created", f"AI draft ready for review: {email['subject']}", "draft", draft_id)
        else:
            activity.log(workspace_id, "draft_failed", f"Draft could not be saved to Gmail: {email['subject']}", "draft", draft_id)
        return {"email_id": email_id, "draft_created": created, "draft_id": draft_id}

    def reprocess(self, workspace_id: str, email_id: str) -> Dict[str, Any]:
        """Runs the AI again for one email, replacing any draft still awaiting review."""
        db = get_db()
        email = db.fetchone("SELECT id, subject FROM emails WHERE id = ? AND workspace_id = ?", (email_id, workspace_id))
        if not email:
            raise NotFound("Email not found.")
        if self.is_busy(email_id):
            raise Conflict("This email is already being processed.")
        drafts = db.fetchall("SELECT id, status FROM drafts WHERE email_id = ?", (email_id,))
        if any(d["status"] in ("approved", "sent") for d in drafts):
            raise Conflict("A reply to this email has already been approved or sent.")

        from app.services.drafts import draft_service
        for d in drafts:
            if d["status"] in ("pending_review", "edited", "failed"):
                draft_service.discard(workspace_id, d["id"], "Replaced by a new AI draft.")
        result = self.process_email(workspace_id, email_id)
        return result

    # --- queries ----------------------------------------------------------
    def list_emails(self, workspace_id: str, status: str = "", query: str = "", limit: int = 200) -> List[Dict[str, Any]]:
        sql = (
            f"SELECT {EMAIL_LIST_COLUMNS}, "
            "(SELECT d.status FROM drafts d WHERE d.email_id = e.id ORDER BY d.created_at DESC LIMIT 1) AS draft_status, "
            "(SELECT d.id FROM drafts d WHERE d.email_id = e.id ORDER BY d.created_at DESC LIMIT 1) AS draft_id "
            "FROM emails e WHERE e.workspace_id = ?"
        )
        params: List[Any] = [workspace_id]
        if status:
            sql += " AND e.status = ?"
            params.append(status)
        if query:
            sql += " AND (e.subject LIKE ? OR e.sender LIKE ? OR e.snippet LIKE ?)"
            like = f"%{query}%"
            params += [like, like, like]
        sql += " ORDER BY e.received_at DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        return get_db().fetchall(sql, params)

    def get_email(self, workspace_id: str, email_id: str, include_thread: bool = True) -> Dict[str, Any]:
        db = get_db()
        email = db.fetchone("SELECT * FROM emails WHERE id = ? AND workspace_id = ?", (email_id, workspace_id))
        if not email:
            raise NotFound("Email not found.")
        email["knowledge_sources"] = loads(email["knowledge_sources"], [])
        email["evidence"] = loads(email.get("evidence"), []) or []
        for key in ("message_id_header", "references_header", "gmail_message_id", "meta"):
            email.pop(key, None)
        email["processing"] = self.is_busy(email_id)
        email["drafts"] = db.fetchall(
            "SELECT id, status, needs_attention, attention_reason, status_message, body, created_at, updated_at "
            "FROM drafts WHERE email_id = ? ORDER BY created_at DESC",
            (email_id,),
        )
        current = next((d for d in email["drafts"] if d["status"] != "rejected"), None)
        email["result"] = {
            "email_id": email_id,
            "question": email.get("question") or "",
            "answer": current["body"] if current else "",
            "sources": [{"document": s} for s in email["knowledge_sources"]],
            "draft_created": bool(current and current["status"] != "failed"),
            "status": current["status"] if current else email["status"],
        }
        email["thread"] = []
        email["thread_error"] = ""
        if include_thread and email.get("account_id") and email.get("thread_id"):
            try:
                service = gmail_accounts.get_service(email["account_id"])
                messages = GmailReader(service=service).get_thread(email["thread_id"])
                email["thread"] = [
                    {
                        "sender": m.sender, "recipient": m.recipient, "date": m.date,
                        "body": m.body, "is_draft": "DRAFT" in m.labels, "is_sent": "SENT" in m.labels,
                    }
                    for m in messages
                ]
            except ServiceError as e:
                email["thread_error"] = e.message
            except Exception:
                logger.exception("Could not load conversation %s", email["thread_id"])
                email["thread_error"] = "The conversation could not be loaded from Gmail."
        return email

    def stats(self, workspace_id: str) -> Dict[str, int]:
        rows = get_db().fetchall(
            "SELECT status, COUNT(*) AS n FROM emails WHERE workspace_id = ? GROUP BY status", (workspace_id,))
        by = {r["status"]: r["n"] for r in rows}
        return {
            "total": sum(by.values()),
            "processed": sum(by.get(k, 0) for k in ("processed", "skipped", "flagged", "needs_attention")),
            "needs_attention": by.get("needs_attention", 0) + by.get("failed", 0),
            "flagged": by.get("flagged", 0),
            "skipped": by.get("skipped", 0),
            "processing": by.get("processing", 0) + by.get("new", 0),
        }


processing_service = ProcessingService()
