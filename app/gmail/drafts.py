"""
Gmail Drafts Module for Intelligent Email Copilot.
Creates, updates and removes drafts in Gmail preserving threading and headers.
STRICT SAFETY ENFORCEMENT: Never sends emails. Drafts are only ever created,
updated or deleted; a human sends them from Gmail.
"""

import base64
import html
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, Optional

from app.gmail.auth import gmail_auth
from app.gmail.reader import EmailMessage

logger = logging.getLogger(__name__)


def reply_subject(subject: str) -> str:
    subject = (subject or "").strip()
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


def build_reply_mime(original_email: EmailMessage, reply_content: str) -> Dict[str, Any]:
    """Builds the Gmail API message body for a threaded reply."""
    mime_message = MIMEMultipart("alternative")
    mime_message["To"] = original_email.sender
    mime_message["Subject"] = reply_subject(original_email.subject)

    if original_email.messageId:
        mime_message["In-Reply-To"] = original_email.messageId
        existing_refs = (original_email.references or "").strip()
        mime_message["References"] = f"{existing_refs} {original_email.messageId}".strip()

    mime_message.attach(MIMEText(reply_content, "plain", "utf-8"))
    html_body = html.escape(reply_content).replace("\n", "<br>")
    html_content = (
        "<!DOCTYPE html><html><head><meta charset='utf-8'></head>"
        "<body><div style='font-family: Arial, sans-serif; font-size: 14px; line-height: 1.6;'>"
        f"{html_body}</div></body></html>"
    )
    mime_message.attach(MIMEText(html_content, "html", "utf-8"))

    message: Dict[str, Any] = {"raw": base64.urlsafe_b64encode(mime_message.as_bytes()).decode("utf-8")}
    if original_email.threadId:
        message["threadId"] = original_email.threadId
    return {"message": message}


class GmailDraftManager:
    """Manages draft replies in Gmail. There is intentionally no send method."""

    def __init__(self, service=None):
        self._service = service

    @property
    def service(self):
        if self._service is None:
            self._service = gmail_auth.get_service()
        return self._service

    def refresh_service(self):
        """Re-initializes the service if credentials have updated."""
        self._service = gmail_auth.get_service()

    def create_draft_reply(
        self,
        original_email: EmailMessage,
        reply_content: str,
        service=None,
    ) -> Optional[Dict[str, Any]]:
        """
        Creates a draft reply attached to the original email thread.
        NOTE: This does NOT send the email. It strictly saves a draft for human review.
        """
        svc = service
        if svc is None:
            if not self.service:
                self.refresh_service()
            svc = self.service
        if not svc:
            logger.warning("Gmail service is unavailable. Cannot create draft in Gmail.")
            return None

        try:
            created_draft = svc.users().drafts().create(
                userId="me", body=build_reply_mime(original_email, reply_content),
            ).execute()
            logger.info(
                "Gmail draft created (Draft ID: %s, Thread ID: %s). Awaiting human review; it will NOT be sent automatically.",
                created_draft.get("id"), original_email.threadId,
            )
            return created_draft
        except Exception as e:
            logger.error("Error creating draft reply in Gmail: %s", e)
            return None

    @staticmethod
    def update_draft(service, draft_id: str, original_email: EmailMessage, reply_content: str) -> Dict[str, Any]:
        """Replaces the content of an existing Gmail draft. Raises on failure."""
        body = build_reply_mime(original_email, reply_content)
        body["id"] = draft_id
        return service.users().drafts().update(userId="me", id=draft_id, body=body).execute()

    @staticmethod
    def get_draft(service, draft_id: str) -> Optional[Dict[str, Any]]:
        """Returns the draft, or None if it no longer exists (sent or deleted)."""
        from googleapiclient.errors import HttpError
        try:
            return service.users().drafts().get(userId="me", id=draft_id, format="minimal").execute()
        except HttpError as e:
            if getattr(e, "resp", None) is not None and e.resp.status == 404:
                return None
            raise

    @staticmethod
    def delete_draft(service, draft_id: str) -> bool:
        """Deletes a draft. Returns False if it was already gone."""
        from googleapiclient.errors import HttpError
        try:
            service.users().drafts().delete(userId="me", id=draft_id).execute()
            return True
        except HttpError as e:
            if getattr(e, "resp", None) is not None and e.resp.status == 404:
                return False
            raise


# Global singleton draft manager
gmail_draft_manager = GmailDraftManager()
