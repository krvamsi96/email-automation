"""
Symmetric encryption for stored secrets (email account OAuth tokens).

The key comes from APP_SECRET_KEY, or is generated once and kept in a local
key file next to the database. Tokens are never returned by the API.
"""

import os
import threading
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from app.config import Config

_fernet: Optional[Fernet] = None
_lock = threading.Lock()


def _load_key() -> bytes:
    if Config.APP_SECRET_KEY:
        return Config.APP_SECRET_KEY.encode()
    key_path = Path(Config.APP_DB_PATH).parent / ".secret_key"
    if key_path.exists():
        return key_path.read_bytes().strip()
    key = Fernet.generate_key()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(key)
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    return key


def _get() -> Fernet:
    global _fernet
    with _lock:
        if _fernet is None:
            _fernet = Fernet(_load_key())
        return _fernet


def encrypt(plaintext: str) -> str:
    return _get().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str) -> Optional[str]:
    try:
        return _get().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None
