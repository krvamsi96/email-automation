"""
AI provider administration: status of each provider, the models it offers,
choosing the active provider / fallbacks / models, and connection tests.

Only safe facts leave this module (whether a key is set, never the key).
"""

import logging
from typing import Any, Dict, Optional

from app.llm import settings as ai_settings
from app.llm.client import llm_client
from app.llm.providers import PROVIDERS, LLMError, get_provider
from app.services.errors import ServiceError

logger = logging.getLogger(__name__)

MAX_MODEL_NAME = 200


class AISettingsService:
    def status(self) -> Dict[str, Any]:
        s = ai_settings.current()
        env = ai_settings.env_defaults()
        return {
            "configured": llm_client.is_configured(),
            "active": llm_client.active(),
            "provider": s.provider,
            "fallbacks": s.fallbacks,
            "customised": bool(ai_settings.stored()),
            "providers": [
                {
                    "name": name,
                    "label": p.label,
                    "configured": p.is_configured(),
                    "needs_key": p.needs_key,
                    "model": s.models.get(name, ""),
                    "default_model": env.models.get(name, ""),
                    "endpoint": p.endpoint,
                    "setup_hint": "" if p.is_configured() else p.setup_hint(),
                }
                for name, p in PROVIDERS.items()
            ],
        }

    def models(self, provider: str, refresh: bool = False) -> Dict[str, Any]:
        p = get_provider(provider)
        if not p:
            raise ServiceError("Unknown AI provider.")
        if not p.is_configured():
            return {"provider": p.name, "models": [], "error": f"{p.label} is not configured. {p.setup_hint()}"}
        try:
            return {"provider": p.name, "models": p.list_models(refresh=refresh), "error": ""}
        except LLMError as e:
            logger.warning("Could not list %s models: %s", p.label, e)
            return {"provider": p.name, "models": [], "error": str(e)}

    def update(self, data: Dict[str, Any]) -> Dict[str, Any]:
        if data.get("reset"):
            ai_settings.reset()
            return self.status()
        current = ai_settings.current()
        provider = str(data.get("provider", current.provider)).strip().lower()
        if provider not in PROVIDERS:
            raise ServiceError("Choose Ollama, Groq or Google Gemini as the AI provider.")
        fallbacks = data.get("fallbacks", current.fallbacks)
        if not isinstance(fallbacks, list) or any(str(f).strip().lower() not in PROVIDERS for f in fallbacks):
            raise ServiceError("Fallback providers must be Ollama, Groq or Google Gemini.")
        models: Dict[str, str] = dict(ai_settings.stored().get("models") or {})
        for name, model in (data.get("models") or {}).items():
            if name not in PROVIDERS:
                raise ServiceError("Unknown AI provider.")
            model = str(model or "").strip()
            if len(model) > MAX_MODEL_NAME or any(c.isspace() for c in model):
                raise ServiceError("Model names cannot contain spaces.")
            models[name] = model
        ai_settings.save(provider, [str(f).strip().lower() for f in fallbacks], models)
        return self.status()

    def test(self, provider: Optional[str] = None, model: Optional[str] = None) -> Dict[str, Any]:
        if provider and not get_provider(provider):
            raise ServiceError("Unknown AI provider.")
        model = (model or "").strip() or None
        result = llm_client.test_connection(provider, model)
        label = PROVIDERS[result["provider"]].label if result.get("provider") in PROVIDERS else "The AI service"
        if result["status"] == "SUCCESS":
            return {"ok": True, "message": f"{label} ({result['model']}) responded in {result['seconds']}s."}
        if result["status"] == "NOT_CONFIGURED":
            return {"ok": False, "message": result["message"]}
        logger.error("AI provider test failed: %s", result.get("error"))
        return {"ok": False, "message": f"{label} did not respond: {result.get('error')}"}


ai_settings_service = AISettingsService()
