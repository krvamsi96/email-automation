"""
In-memory stand-in for the parts of the Gmail API the app uses.

The API wrapper has no working send method; `human_sends_draft` simulates a
person clicking Send in the Gmail web interface.
"""

import base64
import email
import email.utils
import itertools
import time
from typing import Dict

import httplib2
from googleapiclient.errors import HttpError


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


def _not_found():
    return HttpError(httplib2.Response({"status": 404}), b'{"error": {"code": 404}}')


def _public(msg: dict) -> dict:
    return {k: v for k, v in msg.items() if not k.startswith("_")}


class _Call:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class FakeMailbox:
    """Mailbox state plus helpers that simulate what a person does in Gmail."""

    def __init__(self, address: str = "support@example.com"):
        self.address = address
        self.msgs: Dict[str, dict] = {}
        self.drafts: Dict[str, dict] = {}
        self._ids = itertools.count(1000)
        self.send_attempts = 0

    def next_id(self, prefix: str) -> str:
        return f"{prefix}{next(self._ids):x}"

    def add_incoming(self, sender: str, subject: str, body: str, labels=None, headers=None) -> dict:
        msg_id, thread_id = self.next_id("m"), self.next_id("t")
        hdrs = [
            {"name": "From", "value": sender},
            {"name": "To", "value": self.address},
            {"name": "Subject", "value": subject},
            {"name": "Message-ID", "value": f"<{msg_id}@mail.example.com>"},
            {"name": "Date", "value": email.utils.formatdate()},
        ] + [{"name": k, "value": v} for k, v in (headers or {}).items()]
        self.msgs[msg_id] = {
            "id": msg_id, "threadId": thread_id,
            "labelIds": ["INBOX", "UNREAD"] + list(labels or []),
            "internalDate": str(int(time.time() * 1000)),
            "snippet": body[:100],
            "payload": {"mimeType": "text/plain", "headers": hdrs, "body": {"data": _b64(body)}},
        }
        return self.msgs[msg_id]

    def human_sends_draft(self, draft_id: str) -> dict:
        draft = self.drafts.pop(draft_id)
        msg = self.msgs[draft["message"]["id"]]
        msg["labelIds"] = ["SENT"]
        msg["internalDate"] = str(int(time.time() * 1000) + 1000)
        return msg

    def human_deletes_draft(self, draft_id: str) -> None:
        draft = self.drafts.pop(draft_id)
        self.msgs.pop(draft["message"]["id"], None)

    def draft_text(self, draft_id: str) -> str:
        return self.msgs[self.drafts[draft_id]["message"]["id"]]["_plain"]

    def draft_headers(self, draft_id: str) -> dict:
        return self.msgs[self.drafts[draft_id]["message"]["id"]]["_headers"]

    def service(self) -> "FakeService":
        return FakeService(self)


class FakeService:
    def __init__(self, box: FakeMailbox):
        self.box = box

    def users(self):
        return self

    def getProfile(self, userId="me"):
        return _Call(lambda: {"emailAddress": self.box.address, "messagesTotal": len(self.box.msgs), "threadsTotal": 1})

    def messages(self):
        return _Messages(self.box)

    def threads(self):
        return _Threads(self.box)

    def drafts(self):
        return _Drafts(self.box)


class _Messages:
    def __init__(self, box):
        self.box = box

    def list(self, userId="me", q="", maxResults=100, **_):
        def run():
            items = [m for m in self.box.msgs.values() if "INBOX" in m["labelIds"]]
            items.sort(key=lambda m: int(m["internalDate"]), reverse=True)
            return {"messages": [{"id": m["id"], "threadId": m["threadId"]} for m in items[:maxResults]]}
        return _Call(run)

    def get(self, userId="me", id=None, format="full"):
        def run():
            if id not in self.box.msgs:
                raise _not_found()
            return _public(self.box.msgs[id])
        return _Call(run)

    def send(self, *args, **kwargs):
        self.box.send_attempts += 1
        raise AssertionError("The application attempted to send an email through the API")


class _Threads:
    def __init__(self, box):
        self.box = box

    def get(self, userId="me", id=None, format="full", **_):
        def run():
            msgs = [_public(m) for m in self.box.msgs.values() if m["threadId"] == id]
            if not msgs:
                raise _not_found()
            return {"id": id, "messages": sorted(msgs, key=lambda m: int(m["internalDate"]))}
        return _Call(run)


class _Drafts:
    def __init__(self, box):
        self.box = box

    def _store(self, draft_id, body):
        raw = base64.urlsafe_b64decode(body["message"]["raw"].encode())
        parsed = email.message_from_bytes(raw)
        plain = ""
        for part in parsed.walk():
            if part.get_content_type() == "text/plain":
                plain = part.get_payload(decode=True).decode("utf-8")
        thread_id = body["message"].get("threadId") or self.box.next_id("t")
        old = self.box.drafts.get(draft_id)
        msg_id = old["message"]["id"] if old else self.box.next_id("d")
        self.box.msgs[msg_id] = {
            "id": msg_id, "threadId": thread_id, "labelIds": ["DRAFT"],
            "internalDate": str(int(time.time() * 1000)), "snippet": plain[:100],
            "payload": {"mimeType": "text/plain", "headers": [
                {"name": "From", "value": self.box.address}, {"name": "To", "value": parsed["To"] or ""},
                {"name": "Subject", "value": parsed["Subject"] or ""}], "body": {"data": _b64(plain)}},
            "_plain": plain, "_headers": dict(parsed.items()),
        }
        self.box.drafts[draft_id] = {"id": draft_id, "message": {"id": msg_id, "threadId": thread_id}}
        return dict(self.box.drafts[draft_id])

    def create(self, userId="me", body=None):
        return _Call(lambda: self._store(self.box.next_id("r"), body))

    def update(self, userId="me", id=None, body=None):
        def run():
            if id not in self.box.drafts:
                raise _not_found()
            return self._store(id, body)
        return _Call(run)

    def get(self, userId="me", id=None, format="full"):
        def run():
            if id not in self.box.drafts:
                raise _not_found()
            return dict(self.box.drafts[id])
        return _Call(run)

    def delete(self, userId="me", id=None):
        def run():
            if id not in self.box.drafts:
                raise _not_found()
            draft = self.box.drafts.pop(id)
            self.box.msgs.pop(draft["message"]["id"], None)
            return ""
        return _Call(run)

    def list(self, userId="me", **_):
        return _Call(lambda: {"drafts": [dict(d) for d in self.box.drafts.values()]})

    def send(self, *args, **kwargs):
        self.box.send_attempts += 1
        raise AssertionError("The application attempted to send a draft through the API")
