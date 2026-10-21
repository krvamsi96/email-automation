"""
AI providers: Ollama (local), Groq and Google Gemini (hosted).

Each provider turns a list of chat messages ({"role": "system" | "user" |
"assistant", "content": str}) into reply text and lists the chat models it
offers. Providers raise LLMError; `retryable` tells the caller whether trying
the same provider again can help (timeouts, rate limits, server errors) or not
(bad key, unknown model, blocked or cut-off reply).

API keys are sent only in request headers, never in URLs, so they cannot end up
in exception messages or logs; error text is additionally scrubbed of any key.
"""

import logging
import re
import threading
import time
from typing import Any, Dict, List, Optional

import requests

from app.config import Config

logger = logging.getLogger(__name__)

THINK_BLOCK = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)
MODEL_LIST_TTL = 300  # seconds a provider's model list is reused


class LLMError(Exception):
    """An AI request failed. `retryable` is True when the same request may succeed later."""

    def __init__(self, message: str, retryable: bool = False, retry_after: float = 0.0):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


def _secrets() -> List[str]:
    return [k for k in (Config.GROQ_API_KEY, Config.GEMINI_API_KEY) if k]


def redact(text: str) -> str:
    """Removes every configured API key from text that may be shown or logged."""
    for key in _secrets():
        text = text.replace(key, "***")
    return text


def _retry_after(response: requests.Response) -> float:
    try:
        return min(float(response.headers.get("retry-after", "0")), 30.0)
    except ValueError:
        return 0.0


class Provider:
    """Base class. Subclasses implement _chat and _list_models."""

    name = ""
    label = ""
    needs_key = True

    def __init__(self):
        self._models: List[str] = []
        self._models_at = 0.0
        self._lock = threading.Lock()

    # --- configuration ---------------------------------------------------------
    @property
    def default_model(self) -> str:
        raise NotImplementedError

    @property
    def timeout(self) -> float:
        raise NotImplementedError

    @property
    def endpoint(self) -> str:
        raise NotImplementedError

    def is_configured(self) -> bool:
        raise NotImplementedError

    def setup_hint(self) -> str:
        raise NotImplementedError

    # --- requests ------------------------------------------------------------
    def _request(self, method: str, url: str, timeout: float, **kwargs) -> Dict[str, Any]:
        try:
            response = requests.request(method, url, timeout=timeout, **kwargs)
        except requests.exceptions.Timeout as e:
            raise LLMError(f"{self.label} did not answer within {timeout:g}s", retryable=True) from e
        except requests.exceptions.ConnectionError as e:
            raise LLMError(f"{self.label} could not be reached at {self.endpoint}", retryable=True) from e
        except requests.exceptions.RequestException as e:
            raise LLMError(redact(f"{self.label} request failed: {e}"), retryable=True) from e
        if response.status_code >= 400:
            raise self._http_error(response)
        try:
            return response.json()
        except ValueError as e:
            raise LLMError(f"{self.label} returned a response that is not JSON", retryable=True) from e

    def _http_error(self, response: requests.Response) -> LLMError:
        status = response.status_code
        detail = ""
        try:
            body = response.json()
            err = body.get("error") if isinstance(body, dict) else None
            detail = (err.get("message") if isinstance(err, dict) else err) or ""
        except ValueError:
            detail = response.text[:300]
        detail = redact(" ".join(str(detail).split())[:300])
        if status in (401, 403):
            message = f"{self.label} rejected the API key (HTTP {status})"
        elif status == 404:
            message = f"{self.label}: model or address not found (HTTP 404)"
        elif status == 429:
            message = f"{self.label} rate limit reached (HTTP 429)"
        else:
            message = f"{self.label} returned HTTP {status}"
        if detail:
            message += f": {detail}"
        return LLMError(message, retryable=status == 429 or status >= 500, retry_after=_retry_after(response))

    # --- public API ------------------------------------------------------------
    def chat(self, messages: List[Dict[str, str]], model: str = "", temperature: float = 0.0,
             max_tokens: int = 0) -> str:
        if not self.is_configured():
            raise LLMError(f"{self.label} is not configured. {self.setup_hint()}")
        model = model or self.default_model or self.first_model()
        text = self._chat(messages, model, temperature, max_tokens or Config.LLM_MAX_TOKENS)
        text = THINK_BLOCK.sub("", text or "").strip()
        if not text:
            raise LLMError(f"{self.label} returned an empty reply", retryable=True)
        return text

    def list_models(self, refresh: bool = False) -> List[str]:
        """Chat models offered by the provider (cached for a few minutes)."""
        if not self.is_configured():
            return []
        with self._lock:
            if not refresh and self._models and time.time() - self._models_at < MODEL_LIST_TTL:
                return list(self._models)
        models = sorted(set(self._list_models()))
        with self._lock:
            self._models, self._models_at = models, time.time()
        return list(models)

    def first_model(self) -> str:
        models = self.list_models()
        if not models:
            raise LLMError(f"{self.label} has no chat model available. {self.setup_hint()}")
        logger.info("No %s model configured; using %s", self.label, models[0])
        return models[0]

    def _chat(self, messages, model, temperature, max_tokens) -> str:
        raise NotImplementedError

    def _list_models(self) -> List[str]:
        raise NotImplementedError


class OllamaProvider(Provider):
    name = "ollama"
    label = "Ollama"
    needs_key = False

    @property
    def default_model(self) -> str:
        return Config.OLLAMA_MODEL

    @property
    def timeout(self) -> float:
        return Config.OLLAMA_TIMEOUT or Config.LLM_TIMEOUT

    @property
    def endpoint(self) -> str:
        return Config.OLLAMA_BASE_URL

    def is_configured(self) -> bool:
        return bool(Config.OLLAMA_BASE_URL)

    def setup_hint(self) -> str:
        return "Install Ollama, start it and pull a model (for example: ollama pull qwen3:8b); set OLLAMA_BASE_URL in .env."

    def _chat(self, messages, model, temperature, max_tokens) -> str:
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens, "num_ctx": Config.OLLAMA_NUM_CTX},
        }
        if Config.OLLAMA_THINK in ("true", "false"):
            payload["think"] = Config.OLLAMA_THINK == "true"
        try:
            data = self._request("POST", f"{self.endpoint}/api/chat", self.timeout, json=payload)
        except LLMError as e:
            # Models without a reasoning phase reject the "think" option on some Ollama versions
            if "think" in payload and "think" in str(e).lower():
                payload = {k: v for k, v in payload.items() if k != "think"}
                data = self._request("POST", f"{self.endpoint}/api/chat", self.timeout, json=payload)
            else:
                raise
        if data.get("done_reason") == "length":
            raise LLMError(f"{self.label} reply was cut off at the token limit")
        return (data.get("message") or {}).get("content", "")

    def _http_error(self, response: requests.Response) -> LLMError:
        error = super()._http_error(response)
        if response.status_code == 404:
            error = LLMError(f"Ollama model not found. Pull it first (ollama pull <model>). {error}")
        return error

    def _list_models(self) -> List[str]:
        data = self._request("GET", f"{self.endpoint}/api/tags", 10)
        models = []
        for m in data.get("models", []):
            name = m.get("name") or m.get("model")
            if not name:
                continue
            caps = m.get("capabilities")
            if caps is None:
                try:
                    caps = self._request("POST", f"{self.endpoint}/api/show", 10, json={"model": name}).get("capabilities")
                except LLMError:
                    caps = None
            # Embedding-only models cannot chat
            if caps is None or "completion" in caps:
                models.append(name)
        return models


class GroqProvider(Provider):
    name = "groq"
    label = "Groq"
    # Speech, text-to-speech and safety-classifier models cannot write replies
    NON_CHAT = re.compile(r"whisper|tts|orpheus|guard|safeguard", re.IGNORECASE)

    @property
    def default_model(self) -> str:
        return Config.GROQ_MODEL

    @property
    def timeout(self) -> float:
        return Config.GROQ_TIMEOUT or Config.LLM_TIMEOUT

    @property
    def endpoint(self) -> str:
        return Config.GROQ_BASE_URL

    def is_configured(self) -> bool:
        return bool(Config.GROQ_API_KEY and Config.GROQ_BASE_URL)

    def setup_hint(self) -> str:
        return "Set GROQ_API_KEY in .env (create a key at console.groq.com)."

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {Config.GROQ_API_KEY}", "Content-Type": "application/json"}

    def _chat(self, messages, model, temperature, max_tokens) -> str:
        payload: Dict[str, Any] = {"model": model, "messages": messages,
                                   "temperature": temperature, "max_tokens": max_tokens}
        if Config.GROQ_REASONING_EFFORT:
            payload["reasoning_effort"] = Config.GROQ_REASONING_EFFORT
        url = f"{self.endpoint}/chat/completions"
        try:
            data = self._request("POST", url, self.timeout, headers=self._headers(), json=payload)
        except LLMError as e:
            if "reasoning_effort" in payload and "reasoning_effort" in str(e):
                # model without a reasoning phase
                payload = {k: v for k, v in payload.items() if k != "reasoning_effort"}
                data = self._request("POST", url, self.timeout, headers=self._headers(), json=payload)
            else:
                raise
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"{self.label} returned no reply", retryable=True)
        if choices[0].get("finish_reason") == "length":
            raise LLMError(f"{self.label} reply was cut off at the token limit")
        return (choices[0].get("message") or {}).get("content") or ""

    def _list_models(self) -> List[str]:
        data = self._request("GET", f"{self.endpoint}/models", 15, headers=self._headers())
        return [m["id"] for m in data.get("data", [])
                if m.get("id") and m.get("active", True) and not self.NON_CHAT.search(m["id"])]


class GeminiProvider(Provider):
    name = "gemini"
    label = "Google Gemini"
    # Models that generate images, speech or music, or drive tools, rather than write text
    NON_CHAT = re.compile(r"tts|image|banana|lyria|robotics|computer-use|transcribe|embedding|aqa|"
                          r"deep-research|antigravity|customtools|omni", re.IGNORECASE)
    BLOCKED = {"SAFETY", "RECITATION", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "LANGUAGE", "OTHER"}

    @property
    def default_model(self) -> str:
        return Config.GEMINI_MODEL

    @property
    def timeout(self) -> float:
        return Config.GEMINI_TIMEOUT or Config.LLM_TIMEOUT

    @property
    def endpoint(self) -> str:
        return Config.GEMINI_BASE_URL

    def is_configured(self) -> bool:
        return bool(Config.GEMINI_API_KEY and Config.GEMINI_BASE_URL)

    def setup_hint(self) -> str:
        return "Set GEMINI_API_KEY in .env (create a key at aistudio.google.com)."

    def _headers(self) -> Dict[str, str]:
        return {"x-goog-api-key": Config.GEMINI_API_KEY, "Content-Type": "application/json"}

    def _chat(self, messages, model, temperature, max_tokens) -> str:
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        contents = [{"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
                    for m in messages if m["role"] != "system"]
        config: Dict[str, Any] = {"temperature": temperature,
                                  "maxOutputTokens": max_tokens + max(0, Config.GEMINI_THINKING_TOKENS)}
        if Config.GEMINI_THINKING_LEVEL:
            config["thinkingConfig"] = {"thinkingLevel": Config.GEMINI_THINKING_LEVEL}
        payload: Dict[str, Any] = {"contents": contents, "generationConfig": config}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        model_id = model.split("/", 1)[1] if model.startswith("models/") else model
        data = self._request("POST", f"{self.endpoint}/models/{model_id}:generateContent", self.timeout,
                             headers=self._headers(), json=payload)
        block = (data.get("promptFeedback") or {}).get("blockReason")
        if block:
            raise LLMError(f"{self.label} declined the request ({block})")
        candidates = data.get("candidates") or []
        if not candidates:
            raise LLMError(f"{self.label} returned no reply", retryable=True)
        candidate = candidates[0]
        reason = candidate.get("finishReason", "")
        if reason == "MAX_TOKENS":
            raise LLMError(f"{self.label} reply was cut off at the token limit")
        if reason in self.BLOCKED:
            raise LLMError(f"{self.label} declined the request ({reason})")
        parts = (candidate.get("content") or {}).get("parts") or []
        return "".join(p.get("text", "") for p in parts if not p.get("thought"))

    def _list_models(self) -> List[str]:
        models, token = [], ""
        for _ in range(10):
            params = {"pageSize": 1000, **({"pageToken": token} if token else {})}
            data = self._request("GET", f"{self.endpoint}/models", 15, headers=self._headers(), params=params)
            for m in data.get("models", []):
                name = (m.get("name") or "").split("/", 1)[-1]
                if (name and "generateContent" in m.get("supportedGenerationMethods", [])
                        and not self.NON_CHAT.search(name)):
                    models.append(name)
            token = data.get("nextPageToken", "")
            if not token:
                break
        return models


PROVIDERS: Dict[str, Provider] = {p.name: p for p in (OllamaProvider(), GroqProvider(), GeminiProvider())}


def get_provider(name: str) -> Optional[Provider]:
    return PROVIDERS.get((name or "").strip().lower())
