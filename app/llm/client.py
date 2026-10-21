"""
The application's single entry point for AI text generation.

Every request goes to the active provider first and, if it fails, to the
fallback providers in order (see app/llm/settings.py). Each attempt runs in a
worker thread with an overall deadline: an HTTP timeout only limits the wait
between two received bytes, so a provider that answers very slowly could
otherwise hold one call for many minutes. A late attempt is abandoned (its
thread finishes in the background). Temporary failures (timeouts, rate limits,
server errors) are retried LLM_RETRY_COUNT times; permanent ones (bad key,
unknown model) move straight on to the next provider. When no provider answers,
LLMError is raised and the caller hands the email to a person.
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Any, Dict, List, Optional

from app.config import Config
from app.llm import settings as ai_settings
from app.llm.providers import PROVIDERS, LLMError, Provider, get_provider

logger = logging.getLogger(__name__)

_pool = ThreadPoolExecutor(max_workers=32, thread_name_prefix="ai-call")


class LLMClient:
    """Routes AI requests to the configured providers."""

    def settings(self) -> ai_settings.AISettings:
        return ai_settings.current()

    def chain(self) -> List[Provider]:
        """Configured providers in the order they are tried."""
        return [PROVIDERS[n] for n in self.settings().chain if PROVIDERS[n].is_configured()]

    def is_configured(self) -> bool:
        """True when at least one provider in the chain can be called."""
        return bool(self.chain())

    def active(self) -> Dict[str, str]:
        """Provider and model that answer first (for display)."""
        s = self.settings()
        chain = self.chain()
        provider = chain[0] if chain else PROVIDERS[s.provider]
        return {"provider": provider.name, "label": provider.label,
                "model": s.models.get(provider.name) or "auto"}

    def complete(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Reply text for one prompt. Raises LLMError when no provider answers."""
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        return self.chat(messages, temperature=temperature, max_tokens=max_tokens)

    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        s = self.settings()
        chain = [PROVIDERS[n] for n in s.chain if PROVIDERS[n].is_configured()]
        if not chain:
            raise LLMError("No AI provider is configured. Set GROQ_API_KEY or GEMINI_API_KEY in .env, "
                           "or run Ollama locally.")
        temp = Config.LLM_TEMPERATURE if temperature is None else temperature
        tokens = Config.LLM_MAX_TOKENS if max_tokens is None else max_tokens
        errors = []
        for provider in chain:
            try:
                return self._call(provider, s.models.get(provider.name, ""), messages, temp, tokens,
                                  can_fall_back=provider is not chain[-1])
            except LLMError as e:
                errors.append(str(e))
                if provider is not chain[-1]:
                    logger.warning("%s failed (%s); trying the next AI provider", provider.label, e)
        raise LLMError("; ".join(errors))

    def _call(self, provider: Provider, model: str, messages, temperature: float, max_tokens: int,
              can_fall_back: bool = False) -> str:
        model = self.resolve_model(provider, model)
        attempts = 1 + Config.LLM_RETRY_COUNT
        deadline = provider.timeout
        for attempt in range(1, attempts + 1):
            logger.info("AI request to %s (model=%s, attempt %d/%d)", provider.label, model, attempt, attempts)
            future = _pool.submit(provider.chat, messages, model, temperature, max_tokens)
            try:
                return future.result(timeout=deadline)
            except FutureTimeout:
                future.cancel()
                error = LLMError(f"{provider.label} did not answer within {deadline:g}s", retryable=True)
            except LLMError as e:
                error = e
            except Exception as e:  # unexpected reply shape
                error = LLMError(f"{provider.label} returned an unexpected reply: {type(e).__name__}",
                                 retryable=True)
            if not error.retryable or attempt == attempts:
                raise error
            # A provider that asks for a long pause (rate limit) is skipped when another can answer now
            if can_fall_back and error.retry_after > Config.LLM_MAX_RETRY_WAIT:
                raise error
            logger.warning("%s (attempt %d/%d); retrying", error, attempt, attempts)
            time.sleep(error.retry_after or min(2 ** (attempt - 1), 8))
        raise LLMError(f"{provider.label} did not answer")  # not reached

    @staticmethod
    def resolve_model(provider: Provider, model: str = "") -> str:
        """The configured model, else the provider's .env default, else the first model it lists."""
        if model or provider.default_model:
            return model or provider.default_model
        try:
            return provider.first_model()
        except LLMError:
            raise
        except Exception as e:
            raise LLMError(f"{provider.label} model list could not be read: {type(e).__name__}") from e

    def test_connection(self, provider: Optional[str] = None, model: Optional[str] = None) -> Dict[str, Any]:
        """Sends a short request to one provider (default: the first in the chain)."""
        if provider:
            target = get_provider(provider)
            if not target:
                return {"status": "FAILED", "error": f"Unknown AI provider '{provider}'."}
        else:
            chain = self.chain()
            if not chain:
                return {"status": "NOT_CONFIGURED", "message": "No AI provider is configured."}
            target = chain[0]
        if not target.is_configured():
            return {"status": "NOT_CONFIGURED", "provider": target.name,
                    "message": f"{target.label} is not configured. {target.setup_hint()}"}
        model = model or self.settings().models.get(target.name, "")
        started = time.time()
        try:
            model = self.resolve_model(target, model)
            reply = self._call(target, model, [{"role": "user", "content": "Reply with the single word: OK"}],
                               0.0, 300)
        except LLMError as e:
            return {"status": "FAILED", "provider": target.name, "model": model or "auto", "error": str(e)}
        return {"status": "SUCCESS", "provider": target.name, "model": model,
                "message": reply[:200], "seconds": round(time.time() - started, 2)}


# Global singleton client
llm_client = LLMClient()
