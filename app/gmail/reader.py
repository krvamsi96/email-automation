"""
Gmail Inbox Reader Module.
Fetches, decodes, and sanitizes incoming unanswered emails from Gmail.
"""

import base64
import logging
import re
from email.utils import parseaddr, parsedate_to_datetime
from datetime import timezone
from typing import Any, Dict, Iterable, List, Optional
from bs4 import BeautifulSoup

from app.config import Config
from app.gmail.auth import gmail_auth

logger = logging.getLogger(__name__)

PROMOTION_LABELS = {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "SPAM"}


class EmailMessage:
    """Represents a structured email object for processing."""

    def __init__(
        self,
        id: str,
        threadId: str,
        messageId: str,
        references: str,
        sender: str,
        subject: str,
        body: str,
        snippet: str = "",
        recipient: str = "",
        date: str = "",
        labels: Optional[List[str]] = None,
        list_unsubscribe: str = "",
        auto_submitted: str = "",
    ):
        self.id = id
        self.threadId = threadId
        self.messageId = messageId
        self.references = references
        self.sender = sender
        self.subject = subject
        self.body = body
        self.snippet = snippet
        self.recipient = recipient
        self.date = date
        self.labels = labels or []
        self.list_unsubscribe = list_unsubscribe
        self.auto_submitted = auto_submitted  # RFC 3834: set by auto-responders and delivery reports

    @property
    def is_promotional(self) -> bool:
        return bool(self.list_unsubscribe) or bool(PROMOTION_LABELS.intersection(self.labels))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "threadId": self.threadId,
            "messageId": self.messageId,
            "references": self.references,
            "sender": self.sender,
            "subject": self.subject,
            "body": self.body,
            "snippet": self.snippet,
            "recipient": self.recipient,
            "date": self.date,
            "labels": list(self.labels),
            "list_unsubscribe": self.list_unsubscribe,
            "auto_submitted": self.auto_submitted,
        }

    def __repr__(self) -> str:
        return f"<EmailMessage id={self.id} sender={self.sender} subject={self.subject[:30]}>"


def _header_date_iso(value: str, internal_ms: Optional[str]) -> str:
    try:
        if internal_ms:
            from datetime import datetime
            return datetime.fromtimestamp(int(internal_ms) / 1000, tz=timezone.utc).isoformat(timespec="seconds")
        if value:
            dt = parsedate_to_datetime(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError):
        pass
    return ""


def sender_address(sender: str) -> str:
    return parseaddr(sender or "")[1].lower()


class GmailReader:
    """Handles fetching and decoding emails. Works with any authorized Gmail service."""

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

    def fetch_unanswered_emails(
        self,
        max_results: int = 15,
        account_email: str = "",
        lookback_days: Optional[int] = None,
        exclude_ids: Optional[Iterable[str]] = None,
        raise_errors: bool = False,
    ) -> List[EmailMessage]:
        """
        Fetches incoming emails whose thread has no reply and no pending draft yet.
        """
        if not self.service:
            self.refresh_service()
        if not self.service:
            logger.warning("Gmail service is unavailable. Skipping fetch.")
            return []

        own_address = (account_email or Config.MY_EMAIL or "").lower()
        exclude = set(exclude_ids or [])
        try:
            query = "in:inbox -from:me -in:chats"
            if lookback_days:
                query += f" newer_than:{int(lookback_days)}d"
            response = self.service.users().messages().list(
                userId="me", q=query, maxResults=max(1, min(max_results * 2, 100)),
            ).execute()

            messages = response.get("messages", [])
            if not messages:
                logger.info("No incoming inbox emails found.")
                return []

            existing_draft_threads = self._get_existing_draft_thread_ids()
            unanswered: List[EmailMessage] = []
            seen_threads = set()

            for msg_summary in messages:
                if len(unanswered) >= max_results:
                    break
                msg_id = msg_summary["id"]
                thread_id = msg_summary.get("threadId", "")
                if msg_id in exclude or thread_id in seen_threads or thread_id in existing_draft_threads:
                    continue
                seen_threads.add(thread_id)

                if thread_id and self._thread_already_answered(thread_id, msg_id):
                    continue

                email_obj = self.get_email_details(msg_id)
                if not email_obj:
                    continue
                if own_address and sender_address(email_obj.sender) == own_address:
                    continue
                unanswered.append(email_obj)

            logger.info("Retrieved %d unanswered emails ready for processing.", len(unanswered))
            return unanswered

        except Exception as e:
            logger.error("Error fetching unanswered emails from Gmail: %s", e)
            if raise_errors:
                raise
            return []

    def _thread_already_answered(self, thread_id: str, msg_id: str) -> bool:
        """True if the newest message in the thread was sent by the account owner."""
        try:
            thread = self.service.users().threads().get(
                userId="me", id=thread_id, format="minimal"
            ).execute()
            msgs = thread.get("messages", [])
            if not msgs:
                return False
            latest = max(msgs, key=lambda m: int(m.get("internalDate", "0")))
            return latest.get("id") != msg_id and "SENT" in latest.get("labelIds", [])
        except Exception as e:
            logger.warning("Could not inspect thread %s: %s", thread_id, e)
            return False

    def _get_existing_draft_thread_ids(self) -> set:
        """Returns thread IDs of existing drafts in the user's account (all pages)."""
        thread_ids = set()
        try:
            page_token = None
            for _ in range(10):
                kwargs = {"userId": "me", "maxResults": 500}
                if page_token:
                    kwargs["pageToken"] = page_token
                drafts_res = self.service.users().drafts().list(**kwargs).execute()
                for draft in drafts_res.get("drafts", []):
                    t_id = draft.get("message", {}).get("threadId")
                    if t_id:
                        thread_ids.add(t_id)
                page_token = drafts_res.get("nextPageToken")
                if not page_token:
                    break
        except Exception as e:
            logger.warning("Could not fetch existing drafts list: %s", e)
        return thread_ids

    def _parse_message(self, msg: Dict[str, Any]) -> EmailMessage:
        payload = msg.get("payload", {})
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
        return EmailMessage(
            id=msg.get("id", ""),
            threadId=msg.get("threadId", ""),
            messageId=headers.get("message-id", ""),
            references=headers.get("references", ""),
            sender=headers.get("from", "Unknown Sender"),
            subject=headers.get("subject", "") or "(No subject)",
            body=self._extract_clean_body(payload),
            snippet=msg.get("snippet", ""),
            recipient=headers.get("to", ""),
            date=_header_date_iso(headers.get("date", ""), msg.get("internalDate")),
            labels=msg.get("labelIds", []),
            list_unsubscribe=headers.get("list-unsubscribe", ""),
            auto_submitted=headers.get("auto-submitted", ""),
        )

    def get_email_details(self, msg_id: str) -> Optional[EmailMessage]:
        """Fetches full email details and extracts clean text body."""
        try:
            msg = self.service.users().messages().get(userId="me", id=msg_id, format="full").execute()
            return self._parse_message(msg)
        except Exception as e:
            logger.error("Failed to retrieve message details for %s: %s", msg_id, e)
            return None

    def get_thread(self, thread_id: str) -> List[EmailMessage]:
        """Returns every message in a conversation, oldest first."""
        thread = self.service.users().threads().get(userId="me", id=thread_id, format="full").execute()
        msgs = sorted(thread.get("messages", []), key=lambda m: int(m.get("internalDate", "0")))
        return [self._parse_message(m) for m in msgs]

    def _extract_clean_body(self, payload: dict) -> str:
        """Extracts text/plain or cleaned text/html content."""
        def decode_data(data: str) -> str:
            if not data:
                return ""
            try:
                return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="ignore").strip()
            except Exception:
                return ""

        def find(parts: list, mime: str) -> str:
            for part in parts:
                data = part.get("body", {}).get("data", "")
                if part.get("mimeType", "") == mime and data:
                    return decode_data(data)
                if "parts" in part:
                    res = find(part["parts"], mime)
                    if res:
                        return res
            return ""

        parts = payload.get("parts", [])
        if parts:
            body = find(parts, "text/plain")
            if not body:
                html = find(parts, "text/html")
                body = self._html_to_text(html) if html else ""
        else:
            raw = decode_data(payload.get("body", {}).get("data", ""))
            body = self._html_to_text(raw) if payload.get("mimeType") == "text/html" else raw

        body = body.replace("\r\n", "\n")
        body = re.sub(r"[ \t]+", " ", body)
        body = re.sub(r"\n{3,}", "\n\n", body)
        return body.strip()

    def _html_to_text(self, html_content: str) -> str:
        """Strips HTML markup and extracts visible text."""
        soup = BeautifulSoup(html_content, "html.parser")
        for tag in soup(["script", "style", "head", "title", "meta"]):
            tag.decompose()
        return soup.get_text(separator="\n", strip=True)


# Global singleton reader (CLI / legacy single-account mode)
gmail_reader = GmailReader()
