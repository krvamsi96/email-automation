"""
Unit tests for Gmail integration and safety guarantees.
"""

from unittest.mock import MagicMock
from app.gmail.auth import GMAIL_SCOPES
from app.gmail.reader import EmailMessage
from app.gmail.drafts import GmailDraftManager


def test_gmail_scopes():
    assert "https://www.googleapis.com/auth/gmail.modify" in GMAIL_SCOPES
    assert "https://www.googleapis.com/auth/gmail.compose" in GMAIL_SCOPES


def test_email_message_structure():
    msg = EmailMessage(
        id="12345",
        threadId="thread_abc",
        messageId="<msg123@domain.com>",
        references="<ref0@domain.com>",
        sender="customer@example.com",
        subject="Password Help",
        body="I forgot my password.",
    )
    assert msg.id == "12345"
    assert msg.threadId == "thread_abc"
    d = msg.to_dict()
    assert d["sender"] == "customer@example.com"


def test_draft_creation_safety_and_thread_preservation():
    """
    CRITICAL TEST:
    Verify that GmailDraftManager ONLY calls drafts().create()
    and NEVER calls messages().send().
    """
    mock_service = MagicMock()
    mock_drafts = MagicMock()
    mock_messages = MagicMock()

    mock_service.users().drafts.return_value = mock_drafts
    mock_service.users().messages.return_value = mock_messages
    mock_drafts.create.return_value.execute.return_value = {"id": "draft_999"}

    manager = GmailDraftManager()
    manager._service = mock_service

    test_email = EmailMessage(
        id="msg_001",
        threadId="thread_777",
        messageId="<orig@test.com>",
        references="<prev@test.com>",
        sender="user@test.com",
        subject="Help with Account",
        body="How to reset password?",
    )

    draft = manager.create_draft_reply(test_email, "Here are the reset instructions.")

    assert draft is not None
    assert draft["id"] == "draft_999"

    # Verify drafts().create() was called
    mock_drafts.create.assert_called_once()
    create_args, create_kwargs = mock_drafts.create.call_args
    assert create_kwargs["userId"] == "me"
    assert create_kwargs["body"]["message"]["threadId"] == "thread_777"

    # CRITICAL: Verify messages().send() was NEVER called!
    mock_messages.send.assert_not_called()
