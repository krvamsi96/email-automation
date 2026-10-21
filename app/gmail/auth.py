"""
Gmail Authentication Module.
Handles OAuth2 credentials, token persistence, and service creation.
"""

import logging
from pathlib import Path
from typing import Optional
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build, Resource

from app.config import Config

logger = logging.getLogger(__name__)

# Scopes required: reading emails, labeling, and creating drafts
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
]


class GmailAuthenticator:
    """Manages Gmail API credentials and client authentication."""

    def __init__(
        self,
        credentials_path: Optional[str] = None,
        token_path: Optional[str] = None,
    ):
        self.credentials_path = Path(credentials_path or Config.GMAIL_CREDENTIALS_PATH)
        self.token_path = Path(token_path or Config.GMAIL_TOKEN_PATH)

    def is_configured(self) -> bool:
        """Returns True if token exists or credentials file is present."""
        return self.token_path.exists() or self.credentials_path.exists()

    def get_credentials(self) -> Optional[Credentials]:
        """Loads or refreshes Google OAuth2 credentials."""
        creds = None

        if self.token_path.exists():
            try:
                creds = Credentials.from_authorized_user_file(str(self.token_path), GMAIL_SCOPES)
            except Exception as e:
                logger.warning("Existing token.json is invalid or corrupted: %s", e)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    logger.info("Refreshing expired Gmail access token...")
                    creds.refresh(Request())
                    self.save_token(creds)
                except Exception as e:
                    logger.error("Failed to refresh Gmail token: %s", e)
                    creds = None

            if not creds:
                if not self.credentials_path.exists():
                    logger.warning(
                        "Gmail credentials not found at %s. Please provide credentials.json from Google Cloud Console.",
                        self.credentials_path,
                    )
                    return None

                try:
                    logger.info("Initiating OAuth flow using %s...", self.credentials_path)
                    flow = InstalledAppFlow.from_client_secrets_file(
                        str(self.credentials_path), GMAIL_SCOPES
                    )
                    creds = flow.run_local_server(port=0)
                    self.save_token(creds)
                except Exception as e:
                    logger.error("OAuth authentication flow failed: %s", e)
                    return None

        return creds

    def save_token(self, creds: Credentials):
        """Persists the authorized credentials to token.json."""
        try:
            self.token_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.token_path, "w", encoding="utf-8") as token_file:
                token_file.write(creds.to_json())
            logger.info("Saved refreshed credentials to %s", self.token_path)
        except Exception as e:
            logger.error("Failed to save token to %s: %s", self.token_path, e)

    def get_service(self) -> Optional[Resource]:
        """Builds and returns the authorized Gmail API service client."""
        creds = self.get_credentials()
        if not creds:
            return None
        return build("gmail", "v1", credentials=creds)


# Global singleton authenticator
gmail_auth = GmailAuthenticator()
