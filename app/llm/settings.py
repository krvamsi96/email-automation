"""
Which AI provider answers, which ones stand in for it, and each provider's model.

Defaults come from .env (LLM_PROVIDER, LLM_FALLBACK_PROVIDERS, <PROVIDER>_MODEL).
An administrator can override them in Settings -> Advanced; the override is
stored in the application database and applies to every workspace. API keys
are never stored here: they stay in the server environment.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List

from app.config import Config
from app.llm.providers import PROVIDERS
from app.store.db import dumps, get_db, loads, now_iso

KEY = "ai"


@dataclass
class AISettings:
    provider: str
    fallbacks: List[str] = field(default_factory=list)
    models: Dict[str, str] = field(default_factory=dict)  # provider -> model ("" = provider default)

    @property
    def chain(self) -> List[str]:
        """Providers in the order they are tried."""
        return list(dict.fromkeys([self.provider, *self.fallbacks]))

    def to_dict(self) -> Dict[str, Any]:
        return {"provider": self.provider, "fallbacks": list(self.fallbacks), "models": dict(self.models)}


def _names(value: Any) -> List[str]:
    items = value.split(",") if isinstance(value, str) else (value or [])
    return [n for n in dict.fromkeys(str(i).strip().lower() for i in items) if n in PROVIDERS]


def env_defaults() -> AISettings:
    primary = Config.LLM_PROVIDER if Config.LLM_PROVIDER in PROVIDERS else "ollama"
    return AISettings(
        provider=primary,
        fallbacks=[n for n in _names(Config.LLM_FALLBACK_PROVIDERS) if n != primary],
        models={name: p.default_model for name, p in PROVIDERS.items()},
    )


def stored() -> Dict[str, Any]:
    row = get_db().fetchone("SELECT value FROM app_settings WHERE key = ?", (KEY,))
    value = loads(row["value"], {}) if row else {}
    return value if isinstance(value, dict) else {}


def current() -> AISettings:
    """Effective settings: the administrator's choice over the .env defaults."""
    base = env_defaults()
    saved = stored()
    if saved.get("provider") in PROVIDERS:
        base.provider = saved["provider"]
    if "fallbacks" in saved:
        base.fallbacks = _names(saved["fallbacks"])
    for name, model in (saved.get("models") or {}).items():
        if name in PROVIDERS and isinstance(model, str) and model.strip():
            base.models[name] = model.strip()
    base.fallbacks = [n for n in base.fallbacks if n != base.provider]
    return base


def save(provider: str, fallbacks: List[str], models: Dict[str, str]) -> AISettings:
    value = {
        "provider": provider,
        "fallbacks": [n for n in _names(fallbacks) if n != provider],
        "models": {n: (m or "").strip() for n, m in (models or {}).items() if n in PROVIDERS},
    }
    get_db().execute(
        "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (KEY, dumps(value), now_iso()),
    )
    return current()


def reset() -> AISettings:
    """Back to the .env defaults."""
    get_db().execute("DELETE FROM app_settings WHERE key = ?", (KEY,))
    return current()
