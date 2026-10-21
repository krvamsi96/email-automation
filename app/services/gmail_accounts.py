"""
Email account management: Google OAuth (web redirect flow), encrypted token
storage, connection tests and disconnection. Tokens never leave the backend.
"""

import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from app.config import Config
from app.gmail.auth import GMAIL_SCOPES
from app.services import activity
from app.services.errors import NotFound, ServiceError
from app.store import crypto
from app.store.db import get_db, new_id, now_iso

logger = logging.getLogger(__name__)

# Google may return scopes in a different order or include previously granted ones.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

CALLBACK_PATH = "/api/email-accounts/oauth/callback"
STATE_TTL_SECONDS = 600
ACCOUNT_COLUMNS = "id, provider, email, status, status_message, connected_at, last_checked_at"


class GmailAccountService:

    def __init__(self):
        self._states: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    # --- OAuth client configuration (admin) ------------------------------
    def _client_config(self) -> Optional[Dict[str, Any]]:
        path = Path(Config.GMAIL_CREDENTIALS_PATH)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.error("Google OAuth client file at %s is not valid JSON", path)
            return None
        return data if ("web" in data or "installed" in data) else None

    def client_status(self) -> Dict[str, Any]:
        cfg = self._client_config()
        if not cfg:
            return {"configured": False, "client_type": None}
        kind = "web" if "web" in cfg else "installed"
        info = cfg[kind]
        return {
            "configured": True,
            "client_type": kind,
            "project_id": info.get("project_id", ""),
            "redirect_uris": info.get("redirect_uris", []),
        }

    def save_client_config(self, raw: bytes) -> Dict[str, Any]:
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ServiceError("The file is not a valid Google OAuth client JSON file.")
        kind = "web" if "web" in data else "installed" if "installed" in data else None
        if not kind or not data[kind].get("client_id") or not data[kind].get("client_secret"):
            raise ServiceError(
                "The file must be an OAuth client downloaded from Google Cloud Console "
                "(Web application or Desktop app)."
            )
        path = Path(Config.GMAIL_CREDENTIALS_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        return self.client_status()

    # --- OAuth flow ------------------------------------------------------
    def redirect_uri(self, request_base_url: str) -> str:
        base = Config.PUBLIC_BASE_URL or request_base_url.rstrip("/")
        return base + CALLBACK_PATH

    def _prune_states(self):
        cutoff = time.time() - STATE_TTL_SECONDS
        for key in [k for k, v in self._states.items() if v["created"] < cutoff]:
            self._states.pop(key, None)

    def start_oauth(self, workspace_id: str, request_base_url: str) -> str:
        cfg = self._client_config()
        if not cfg:
            raise ServiceError(
                "Gmail sign-in has not been set up by your administrator yet.",
                code="oauth_not_configured",
            )
        redirect_uri = self.redirect_uri(request_base_url)
        host = urlparse(redirect_uri).hostname or ""
        if urlparse(redirect_uri).scheme == "http" and host in ("localhost", "127.0.0.1", "::1"):
            os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"  # loopback only

        flow = Flow.from_client_config(cfg, scopes=GMAIL_SCOPES, redirect_uri=redirect_uri)
        state = secrets.token_urlsafe(24)
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            prompt="consent",
            include_granted_scopes="true",
            state=state,
        )
        with self._lock:
            self._prune_states()
            self._states[state] = {
                "workspace_id": workspace_id,
                "code_verifier": flow.code_verifier,
                "redirect_uri": redirect_uri,
                "created": time.time(),
            }
        return auth_url

    def complete_oauth(self, state: str, code: str) -> Dict[str, Any]:
        with self._lock:
            self._prune_states()
            pending = self._states.pop(state or "", None)
        if not pending:
            raise ServiceError("This sign-in link has expired. Please try connecting again.", code="oauth_state")
        cfg = self._client_config()
        if not cfg:
            raise ServiceError("Gmail sign-in is not configured.", code="oauth_not_configured")

        flow = Flow.from_client_config(cfg, scopes=GMAIL_SCOPES, redirect_uri=pending["redirect_uri"], state=state)
        flow.code_verifier = pending["code_verifier"]
        try:
            flow.fetch_token(code=code)
        except Exception as e:
            logger.error("OAuth token exchange failed: %s", e)
            raise ServiceError("Google sign-in could not be completed. Please try again.", code="oauth_exchange")

        creds = flow.credentials
        granted_raw = creds.granted_scopes or creds.scopes or []
        granted = set(granted_raw.split() if isinstance(granted_raw, str) else granted_raw)
        if granted and not set(GMAIL_SCOPES).issubset(granted):
            raise ServiceError(
                "Please allow all requested Gmail permissions so drafts can be created.",
                code="oauth_scopes",
            )
        if not creds.refresh_token:
            logger.warning("Google did not return a refresh token; access will expire in about an hour.")

        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        try:
            profile = service.users().getProfile(userId="me").execute()
        except Exception as e:
            logger.error("Connection verification failed after OAuth: %s", e)
            raise ServiceError("Connected to Google, but Gmail could not be reached.", code="verify_failed")

        return self._store_account(pending["workspace_id"], profile["emailAddress"].lower(), creds)

    def _store_account(self, workspace_id: str, email: str, creds: Credentials) -> Dict[str, Any]:
        db = get_db()
        token = crypto.encrypt(creds.to_json())
        existing = db.fetchone(
            "SELECT id FROM email_accounts WHERE workspace_id = ? AND email = ?", (workspace_id, email))
        if existing:
            db.update("email_accounts", existing["id"], {
                "token_encrypted": token, "status": "connected", "status_message": "",
                "connected_at": now_iso(),
            })
            account_id = existing["id"]
            activity.log(workspace_id, "account_reconnected", f"Email account reconnected: {email}", "account", account_id)
        else:
            account_id = new_id()
            db.insert("email_accounts", {
                "id": account_id,
                "workspace_id": workspace_id,
                "provider": "gmail",
                "email": email,
                "token_encrypted": token,
                "status": "connected",
                "status_message": "",
                "connected_at": now_iso(),
            })
            activity.log(workspace_id, "account_connected", f"Email account connected: {email}", "account", account_id)
        return self.get(workspace_id, account_id)

    # --- accounts --------------------------------------------------------
    def list(self, workspace_id: str) -> List[Dict[str, Any]]:
        return get_db().fetchall(
            f"SELECT {ACCOUNT_COLUMNS} FROM email_accounts WHERE workspace_id = ? ORDER BY connected_at",
            (workspace_id,),
        )

    def get(self, workspace_id: str, account_id: str) -> Dict[str, Any]:
        row = get_db().fetchone(
            f"SELECT {ACCOUNT_COLUMNS} FROM email_accounts WHERE id = ? AND workspace_id = ?",
            (account_id, workspace_id),
        )
        if not row:
            raise NotFound("Email account not found. It may have been disconnected.")
        return row

    def _set_status(self, account_id: str, status: str, message: str = ""):
        get_db().update("email_accounts", account_id, {"status": status, "status_message": message})

    def get_credentials(self, account_id: str) -> Credentials:
        row = get_db().fetchone("SELECT id, token_encrypted FROM email_accounts WHERE id = ?", (account_id,))
        if not row:
            raise ServiceError("This email account is no longer connected.", code="account_missing")
        raw = crypto.decrypt(row["token_encrypted"] or "")
        if not raw:
            self._set_status(account_id, "disconnected", "Stored sign-in could not be read. Please reconnect.")
            raise ServiceError("Gmail connection failed. Please reconnect your account.", code="reconnect")
        creds = Credentials.from_authorized_user_info(json.loads(raw), GMAIL_SCOPES)
        if not creds.valid:
            if not creds.refresh_token:
                self._set_status(account_id, "disconnected", "Sign-in expired. Please reconnect.")
                raise ServiceError("Gmail connection failed. Please reconnect your account.", code="reconnect")
            try:
                creds.refresh(Request())
            except RefreshError as e:
                logger.warning("Token refresh failed for account %s: %s", account_id, e)
                self._set_status(account_id, "disconnected", "Access was revoked or expired. Please reconnect.")
                raise ServiceError("Gmail connection failed. Please reconnect your account.", code="reconnect")
            except Exception as e:
                logger.warning("Token refresh network error for account %s: %s", account_id, e)
                raise ServiceError("Gmail could not be reached. Check your internet connection.", code="network")
            get_db().update("email_accounts", account_id, {"token_encrypted": crypto.encrypt(creds.to_json())})
        return creds

    def get_service(self, account_id: str):
        return build("gmail", "v1", credentials=self.get_credentials(account_id), cache_discovery=False)

    def test(self, workspace_id: str, account_id: str) -> Dict[str, Any]:
        account = self.get(workspace_id, account_id)
        try:
            service = self.get_service(account_id)
            profile = service.users().getProfile(userId="me").execute()
            service.users().drafts().list(userId="me", maxResults=1).execute()
        except ServiceError:
            raise
        except Exception as e:
            logger.error("Connection test failed for %s: %s", account["email"], e)
            self._set_status(account_id, "error", "Gmail did not respond. Please try again or reconnect.")
            raise ServiceError("Gmail connection failed. Please reconnect your account.", code="reconnect")
        if profile.get("emailAddress", "").lower() != account["email"]:
            self._set_status(account_id, "error", "Signed in to a different Google account.")
            raise ServiceError("This connection belongs to a different Gmail address. Please reconnect.", code="reconnect")
        self._set_status(account_id, "connected", "")
        activity.log(workspace_id, "account_tested", f"Connection verified: {account['email']}", "account", account_id)
        return {
            "ok": True,
            "email": account["email"],
            "messages_total": profile.get("messagesTotal", 0),
            "threads_total": profile.get("threadsTotal", 0),
            "account": self.get(workspace_id, account_id),
        }

    def disconnect(self, workspace_id: str, account_id: str) -> Dict[str, Any]:
        account = self.get(workspace_id, account_id)
        row = get_db().fetchone("SELECT token_encrypted FROM email_accounts WHERE id = ?", (account_id,))
        raw = crypto.decrypt((row or {}).get("token_encrypted") or "")
        revoked = False
        if raw:
            token = json.loads(raw)
            to_revoke = token.get("refresh_token") or token.get("token")
            if to_revoke:
                try:
                    resp = requests.post(
                        "https://oauth2.googleapis.com/revoke",
                        params={"token": to_revoke},
                        headers={"content-type": "application/x-www-form-urlencoded"},
                        timeout=10,
                    )
                    revoked = resp.status_code == 200
                except requests.RequestException as e:
                    logger.warning("Token revocation request failed: %s", e)
        # Stored credentials are removed regardless of whether Google confirmed revocation.
        get_db().execute("DELETE FROM email_accounts WHERE id = ?", (account_id,))
        activity.log(workspace_id, "account_disconnected", f"Email account disconnected: {account['email']}", "account", account_id)
        return {"id": account_id, "email": account["email"], "access_revoked": revoked}

    def import_legacy_token(self, workspace_id: str) -> None:
        """Adopts a token.json from the command-line tool as a connected account."""
        path = Path(Config.GMAIL_TOKEN_PATH)
        if not path.exists() or self.list(workspace_id):
            return
        try:
            creds = Credentials.from_authorized_user_file(str(path), GMAIL_SCOPES)
            if not creds.valid and creds.refresh_token:
                creds.refresh(Request())
            service = build("gmail", "v1", credentials=creds, cache_discovery=False)
            email = service.users().getProfile(userId="me").execute()["emailAddress"].lower()
            self._store_account(workspace_id, email, creds)
            logger.info("Imported existing Gmail sign-in for %s", email)
        except Exception as e:
            logger.warning("Could not import existing Gmail token: %s", e)


gmail_accounts = GmailAccountService()
