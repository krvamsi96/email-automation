"""
Workspaces: one per client. Each has its own branding, settings, email accounts,
knowledge base, emails and drafts.
"""

import copy
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import Config
from app.rag.vector_store import DEFAULT_WORKSPACE_ID, get_vector_store_manager
from app.services.errors import NotFound, ServiceError
from app.store.db import dumps, get_db, loads, new_id, now_iso

TONES = ["professional", "friendly", "formal", "concise"]
LOGO_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".svg": "image/svg+xml", ".webp": "image/webp"}
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def default_settings() -> Dict[str, Any]:
    return {
        "branding": {
            "product_name": Config.PRODUCT_NAME,
            "company_name": Config.COMPANY_NAME,
            "support_email": "",
            "primary_color": "#4f46e5",
            "logo_file": "",
        },
        "email": {
            "auto_check_minutes": 0,
            "lookback_days": 3,
            "max_per_check": Config.MAX_EMAILS_PER_RUN,
            "skip_promotions": True,
        },
        "ai": {
            "tone": "professional",
            "signature": "",
            "draft_when_unknown": True,
        },
        "advanced": {
            "min_relevance": Config.RETRIEVAL_MIN_RELEVANCE,
        },
    }


def _merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for section, values in (override or {}).items():
        if section in out and isinstance(values, dict):
            for key, val in values.items():
                if key in out[section]:
                    out[section][key] = val
    return out


def _clamp_int(value: Any, lo: int, hi: int, field: str) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ServiceError(f"{field} must be a whole number.")
    if n < lo or n > hi:
        raise ServiceError(f"{field} must be between {lo} and {hi}.")
    return n


def validate_settings(patch: Dict[str, Any], allow_advanced: bool) -> Dict[str, Any]:
    """Validates a partial settings update and returns the cleaned patch."""
    clean: Dict[str, Dict[str, Any]] = {}
    if not isinstance(patch, dict):
        raise ServiceError("Invalid settings payload.")

    b = patch.get("branding")
    if isinstance(b, dict):
        out = {}
        for key, limit in (("product_name", 60), ("company_name", 80)):
            if key in b:
                val = str(b[key] or "").strip()
                if not val:
                    raise ServiceError("Product and company name cannot be empty.")
                out[key] = val[:limit]
        if "support_email" in b:
            val = str(b["support_email"] or "").strip()
            if val and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", val):
                raise ServiceError("Support email is not a valid email address.")
            out["support_email"] = val
        if "primary_color" in b:
            val = str(b["primary_color"] or "").strip()
            if not COLOR_RE.match(val):
                raise ServiceError("Brand color must be a hex color such as #4f46e5.")
            out["primary_color"] = val.lower()
        clean["branding"] = out

    e = patch.get("email")
    if isinstance(e, dict):
        out = {}
        if "auto_check_minutes" in e:
            n = _clamp_int(e["auto_check_minutes"], 0, 1440, "Automatic check interval")
            if 0 < n < 2:
                raise ServiceError("Automatic check interval must be 0 (off) or at least 2 minutes.")
            out["auto_check_minutes"] = n
        if "lookback_days" in e:
            out["lookback_days"] = _clamp_int(e["lookback_days"], 1, 30, "Look-back window")
        if "max_per_check" in e:
            out["max_per_check"] = _clamp_int(e["max_per_check"], 1, 50, "Emails per check")
        if "skip_promotions" in e:
            out["skip_promotions"] = bool(e["skip_promotions"])
        clean["email"] = out

    a = patch.get("ai")
    if isinstance(a, dict):
        out = {}
        if "tone" in a:
            if a["tone"] not in TONES:
                raise ServiceError("Unknown response tone.")
            out["tone"] = a["tone"]
        if "signature" in a:
            out["signature"] = str(a["signature"] or "").strip()[:500]
        if "draft_when_unknown" in a:
            out["draft_when_unknown"] = bool(a["draft_when_unknown"])
        clean["ai"] = out

    adv = patch.get("advanced")
    if isinstance(adv, dict):
        if not allow_advanced:
            raise ServiceError("Administrator access is required for advanced settings.", status=403)
        out = {}
        if "min_relevance" in adv:
            try:
                val = float(adv["min_relevance"])
            except (TypeError, ValueError):
                raise ServiceError("Match threshold must be a number.")
            if not 0.0 <= val <= 0.95:
                raise ServiceError("Match threshold must be between 0 and 0.95.")
            out["min_relevance"] = round(val, 2)
        clean["advanced"] = out
    return clean


class WorkspaceService:

    def ensure_default(self) -> None:
        db = get_db()
        if not db.fetchone("SELECT id FROM workspaces WHERE id = ?", (DEFAULT_WORKSPACE_ID,)):
            db.insert("workspaces", {
                "id": DEFAULT_WORKSPACE_ID,
                "name": Config.COMPANY_NAME,
                "settings": dumps({}),
                "created_at": now_iso(),
            })

    def list(self) -> List[Dict[str, Any]]:
        rows = get_db().fetchall("SELECT id, name, created_at FROM workspaces ORDER BY created_at, name")
        return rows

    def get(self, workspace_id: str) -> Dict[str, Any]:
        row = get_db().fetchone("SELECT * FROM workspaces WHERE id = ?", (workspace_id,))
        if not row:
            raise NotFound("Workspace not found.")
        row["settings"] = _merge(default_settings(), loads(row["settings"], {}))
        return row

    def exists(self, workspace_id: str) -> bool:
        return bool(get_db().fetchone("SELECT id FROM workspaces WHERE id = ?", (workspace_id,)))

    def settings(self, workspace_id: str) -> Dict[str, Any]:
        return self.get(workspace_id)["settings"]

    def update_settings(self, workspace_id: str, patch: Dict[str, Any], allow_advanced: bool) -> Dict[str, Any]:
        clean = validate_settings(patch, allow_advanced)
        row = get_db().fetchone("SELECT settings FROM workspaces WHERE id = ?", (workspace_id,))
        if not row:
            raise NotFound("Workspace not found.")
        stored = loads(row["settings"], {})
        for section, values in clean.items():
            stored.setdefault(section, {}).update(values)
        updates = {"settings": dumps(stored)}
        company = clean.get("branding", {}).get("company_name")
        if company:
            updates["name"] = company
        get_db().update("workspaces", workspace_id, updates)
        return self.settings(workspace_id)

    def create(self, name: str) -> Dict[str, Any]:
        name = (name or "").strip()
        if not name:
            raise ServiceError("Workspace name is required.")
        ws_id = new_id()
        get_db().insert("workspaces", {
            "id": ws_id,
            "name": name[:80],
            "settings": dumps({"branding": {"company_name": name[:80]}}),
            "created_at": now_iso(),
        })
        self.documents_dir(ws_id)
        return {"id": ws_id, "name": name[:80]}

    def delete(self, workspace_id: str) -> None:
        if workspace_id == DEFAULT_WORKSPACE_ID:
            raise ServiceError("The default workspace cannot be deleted.")
        if not self.exists(workspace_id):
            raise NotFound("Workspace not found.")
        from app.services.gmail_accounts import gmail_accounts
        for account in gmail_accounts.list(workspace_id):
            gmail_accounts.disconnect(workspace_id, account["id"])
        db = get_db()
        for table in ("drafts", "emails", "documents", "activity", "email_accounts", "knowledge_tests"):
            db.execute(f"DELETE FROM {table} WHERE workspace_id = ?", (workspace_id,))
        db.execute("DELETE FROM workspaces WHERE id = ?", (workspace_id,))
        get_vector_store_manager(workspace_id).reset()
        shutil.rmtree(self.base_dir(workspace_id), ignore_errors=True)

    # --- file locations -------------------------------------------------
    def base_dir(self, workspace_id: str) -> Path:
        return Path(Config.WORKSPACES_DIR) / workspace_id

    def documents_dir(self, workspace_id: str) -> Path:
        if workspace_id == DEFAULT_WORKSPACE_ID:
            path = Path(Config.KNOWLEDGE_BASE_PATH)
        else:
            path = self.base_dir(workspace_id) / "documents"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def assets_dir(self, workspace_id: str) -> Path:
        path = self.base_dir(workspace_id) / "assets"
        path.mkdir(parents=True, exist_ok=True)
        return path

    # --- branding -------------------------------------------------------
    def save_logo(self, workspace_id: str, filename: str, data: bytes) -> Dict[str, Any]:
        ext = Path(filename or "").suffix.lower()
        if ext not in LOGO_TYPES:
            raise ServiceError("Logo must be a PNG, JPG, SVG or WebP image.")
        if len(data) > 2 * 1024 * 1024:
            raise ServiceError("Logo must be smaller than 2 MB.")
        if not data:
            raise ServiceError("The logo file is empty.")
        assets = self.assets_dir(workspace_id)
        for old in assets.glob("logo.*"):
            old.unlink(missing_ok=True)
        (assets / f"logo{ext}").write_bytes(data)
        return self._set_logo(workspace_id, f"logo{ext}")

    def remove_logo(self, workspace_id: str) -> Dict[str, Any]:
        for old in self.assets_dir(workspace_id).glob("logo.*"):
            old.unlink(missing_ok=True)
        return self._set_logo(workspace_id, "")

    def _set_logo(self, workspace_id: str, logo_file: str) -> Dict[str, Any]:
        row = get_db().fetchone("SELECT settings FROM workspaces WHERE id = ?", (workspace_id,))
        stored = loads(row["settings"], {}) if row else {}
        stored.setdefault("branding", {})["logo_file"] = logo_file
        get_db().update("workspaces", workspace_id, {"settings": dumps(stored)})
        return self.settings(workspace_id)

    def logo_path(self, workspace_id: str) -> Optional[Path]:
        logo = self.settings(workspace_id)["branding"].get("logo_file")
        if not logo:
            return None
        path = self.assets_dir(workspace_id) / logo
        return path if path.exists() else None


workspace_service = WorkspaceService()
