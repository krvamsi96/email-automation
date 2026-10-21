"""
AI provider layer: Ollama, Groq and Gemini request/response handling, the
fallback chain, the per-attempt deadline, runtime provider/model selection and
API-key hygiene. No real network calls: HTTP is mocked.
"""

import time
from unittest.mock import MagicMock, patch

import pytest
import requests

from app.config import Config
from app.llm import settings as ai_settings
from app.llm.client import llm_client
from app.llm.providers import PROVIDERS, LLMError, redact

GROQ_KEY = "gsk_unit_test_key_123"
GEMINI_KEY = "AIza_unit_test_key_456"


def response(status=200, json=None, headers=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = json if json is not None else {}
    r.text = str(json)
    r.headers = headers or {}
    return r


@pytest.fixture
def keys():
    with patch.object(Config, "GROQ_API_KEY", GROQ_KEY), \
         patch.object(Config, "GEMINI_API_KEY", GEMINI_KEY), \
         patch.object(Config, "OLLAMA_BASE_URL", "http://ollama.test:11434"), \
         patch.object(Config, "GROQ_MODEL", "groq-model"), \
         patch.object(Config, "GEMINI_MODEL", "gemini-model"), \
         patch.object(Config, "OLLAMA_MODEL", "ollama-model"), \
         patch.object(Config, "LLM_RETRY_COUNT", 1):
        yield
    ai_settings.reset()


def use(provider, *fallbacks):
    ai_settings.save(provider, list(fallbacks), {})


# --------------------------------------------------------------------- providers
def test_groq_request_and_reply(keys):
    reply = {"choices": [{"message": {"content": "Hello from Groq"}, "finish_reason": "stop"}]}
    with patch("requests.request", return_value=response(json=reply)) as req:
        assert PROVIDERS["groq"].chat([{"role": "user", "content": "hi"}], "groq-model", 0.1, 100) == "Hello from Groq"
    method, url = req.call_args.args
    kwargs = req.call_args.kwargs
    assert method == "POST" and url == f"{Config.GROQ_BASE_URL}/chat/completions"
    assert kwargs["headers"]["Authorization"] == f"Bearer {GROQ_KEY}"
    assert GROQ_KEY not in url
    assert kwargs["json"]["model"] == "groq-model" and kwargs["json"]["max_tokens"] == 100


def test_groq_retries_without_reasoning_effort_for_models_without_reasoning(keys):
    bad = response(400, {"error": {"message": "`reasoning_effort` is not supported with this model"}})
    good = response(json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})
    with patch.object(Config, "GROQ_REASONING_EFFORT", "low"), patch("requests.request", side_effect=[bad, good]) as req:
        assert PROVIDERS["groq"].chat([{"role": "user", "content": "hi"}], "m") == "ok"
    assert "reasoning_effort" in req.call_args_list[0].kwargs["json"]
    assert "reasoning_effort" not in req.call_args_list[1].kwargs["json"]


def test_gemini_maps_system_and_roles_and_skips_thoughts(keys):
    reply = {"candidates": [{"finishReason": "STOP", "content": {"parts": [
        {"text": "thinking...", "thought": True}, {"text": "Final answer"}]}}]}
    messages = [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "Q1"},
                {"role": "assistant", "content": "A1"}, {"role": "user", "content": "Q2"}]
    with patch("requests.request", return_value=response(json=reply)) as req:
        assert PROVIDERS["gemini"].chat(messages, "models/gemini-model", 0.0, 100) == "Final answer"
    url, kwargs = req.call_args.args[1], req.call_args.kwargs
    assert url.endswith("/models/gemini-model:generateContent") and GEMINI_KEY not in url
    assert kwargs["headers"]["x-goog-api-key"] == GEMINI_KEY
    body = kwargs["json"]
    assert body["systemInstruction"]["parts"][0]["text"] == "Be brief."
    assert [c["role"] for c in body["contents"]] == ["user", "model", "user"]
    # thinking counts against the output limit, so head-room is added
    assert body["generationConfig"]["maxOutputTokens"] == 100 + Config.GEMINI_THINKING_TOKENS


@pytest.mark.parametrize("name,reply", [
    ("groq", {"choices": [{"message": {"content": "partial"}, "finish_reason": "length"}]}),
    ("gemini", {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "partial"}]}}]}),
    ("ollama", {"message": {"content": "partial"}, "done_reason": "length"}),
])
def test_cut_off_replies_are_errors_not_drafts(keys, name, reply):
    with patch("requests.request", return_value=response(json=reply)):
        with pytest.raises(LLMError, match="cut off") as err:
            PROVIDERS[name].chat([{"role": "user", "content": "hi"}], "m")
    assert not err.value.retryable


def test_gemini_safety_block_is_reported(keys):
    with patch("requests.request", return_value=response(json={"promptFeedback": {"blockReason": "SAFETY"}})):
        with pytest.raises(LLMError, match="declined"):
            PROVIDERS["gemini"].chat([{"role": "user", "content": "hi"}], "m")


def test_ollama_request_strips_thinking(keys):
    reply = {"message": {"content": "<think>pondering</think>\nThe answer"}, "done_reason": "stop"}
    with patch.object(Config, "OLLAMA_THINK", "false"), patch("requests.request", return_value=response(json=reply)) as req:
        assert PROVIDERS["ollama"].chat([{"role": "user", "content": "hi"}], "qwen3:8b", 0.2, 50) == "The answer"
    body = req.call_args.kwargs["json"]
    assert req.call_args.args[1] == "http://ollama.test:11434/api/chat"
    assert body["think"] is False and body["stream"] is False
    assert body["options"]["num_predict"] == 50 and body["options"]["num_ctx"] == Config.OLLAMA_NUM_CTX


def test_unreachable_provider_is_a_retryable_error(keys):
    with patch("requests.request", side_effect=requests.exceptions.ConnectionError("refused")):
        with pytest.raises(LLMError, match="could not be reached") as err:
            PROVIDERS["ollama"].chat([{"role": "user", "content": "hi"}], "m")
    assert err.value.retryable


def test_bad_key_is_permanent_and_never_echoed(keys):
    bad = response(401, {"error": {"message": f"Invalid API Key {GROQ_KEY}"}})
    with patch("requests.request", return_value=bad):
        with pytest.raises(LLMError) as err:
            PROVIDERS["groq"].chat([{"role": "user", "content": "hi"}], "m")
    assert not err.value.retryable
    assert "rejected the API key" in str(err.value) and GROQ_KEY not in str(err.value)
    assert redact(f"x {GEMINI_KEY} y") == "x *** y"


def test_model_lists_skip_non_chat_models(keys):
    groq = response(json={"data": [{"id": "openai/gpt-oss-120b"}, {"id": "whisper-large-v3"},
                                   {"id": "meta-llama/llama-prompt-guard-2-22m"}]})
    gemini = response(json={"models": [
        {"name": "models/gemini-3.5-flash", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-3.8-flash-tts", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]}]})
    ollama = response(json={"models": [{"name": "qwen3:8b", "capabilities": ["completion", "thinking"]},
                                       {"name": "nomic-embed-text:latest", "capabilities": ["embedding"]}]})
    with patch("requests.request", side_effect=[groq, gemini, ollama]):
        assert PROVIDERS["groq"].list_models(refresh=True) == ["openai/gpt-oss-120b"]
        assert PROVIDERS["gemini"].list_models(refresh=True) == ["gemini-3.5-flash"]
        assert PROVIDERS["ollama"].list_models(refresh=True) == ["qwen3:8b"]


# --------------------------------------------------------------------- client
def test_nothing_configured_raises_clear_error():
    assert not llm_client.is_configured()
    with pytest.raises(LLMError, match="No AI provider is configured"):
        llm_client.complete("hi")


def test_fallback_provider_answers_when_primary_fails(keys):
    use("groq", "gemini")
    with patch.object(PROVIDERS["groq"], "chat", side_effect=LLMError("Groq rejected the API key")) as groq, \
         patch.object(PROVIDERS["gemini"], "chat", return_value="from gemini") as gemini:
        assert llm_client.complete("hi", system_prompt="sys", temperature=0.0) == "from gemini"
    groq.assert_called_once()  # permanent error: not retried
    messages, model, temperature, _ = gemini.call_args.args
    assert messages[0] == {"role": "system", "content": "sys"} and model == "gemini-model" and temperature == 0.0


def test_all_providers_failing_reports_each(keys):
    use("groq", "gemini")
    with patch.object(PROVIDERS["groq"], "chat", side_effect=LLMError("groq down")), \
         patch.object(PROVIDERS["gemini"], "chat", side_effect=LLMError("gemini down")):
        with pytest.raises(LLMError, match="groq down; gemini down"):
            llm_client.complete("hi")


def test_temporary_errors_are_retried(keys):
    use("groq")
    with patch.object(PROVIDERS["groq"], "chat", side_effect=[LLMError("429", retryable=True, retry_after=0.01), "ok"]) as groq:
        assert llm_client.complete("hi") == "ok"
    assert groq.call_count == 2


def test_long_rate_limit_pause_moves_to_fallback_instead_of_waiting(keys):
    use("groq", "gemini")
    limited = LLMError("Groq rate limit reached (HTTP 429)", retryable=True, retry_after=20)
    with patch.object(PROVIDERS["groq"], "chat", side_effect=limited) as groq, \
         patch.object(PROVIDERS["gemini"], "chat", return_value="from gemini"):
        start = time.time()
        assert llm_client.complete("hi") == "from gemini"
        assert time.time() - start < 1
    groq.assert_called_once()
    # Without a fallback the pause is waited out (capped by the provider) and the call retried
    use("groq")
    with patch.object(PROVIDERS["groq"], "chat", side_effect=[LLMError("429", retryable=True, retry_after=0.05), "ok"]):
        assert llm_client.complete("hi") == "ok"


def test_slow_provider_is_abandoned_at_the_deadline(keys):
    """A provider that answers too slowly is given up instead of blocking the workflow."""
    use("groq", "gemini")
    calls = []

    def slow(*args):
        calls.append(args)
        time.sleep(2)
        return "late"

    with patch.object(Config, "GROQ_TIMEOUT", 0.2), patch.object(Config, "LLM_RETRY_COUNT", 0), \
         patch.object(PROVIDERS["groq"], "chat", side_effect=slow), \
         patch.object(PROVIDERS["gemini"], "chat", return_value="fast"):
        start = time.time()
        assert llm_client.complete("hi") == "fast"
        assert time.time() - start < 1.5
    assert len(calls) == 1


def test_unconfigured_providers_are_skipped(keys):
    use("gemini", "groq")
    with patch.object(Config, "GEMINI_API_KEY", ""), patch.object(PROVIDERS["groq"], "chat", return_value="groq") as groq:
        assert llm_client.chain() == [PROVIDERS["groq"]]
        assert llm_client.complete("hi") == "groq"
    groq.assert_called_once()


# --------------------------------------------------------------------- settings
def test_runtime_settings_override_env_and_reset(keys):
    with patch.object(Config, "LLM_PROVIDER", "groq"), patch.object(Config, "LLM_FALLBACK_PROVIDERS", "gemini,bogus"):
        ai_settings.reset()
        s = ai_settings.current()
        assert s.chain == ["groq", "gemini"] and s.models["groq"] == "groq-model"
        s = ai_settings.save("ollama", ["groq", "ollama", "nope"], {"ollama": "llama3.2:3b", "groq": ""})
        assert s.chain == ["ollama", "groq"]
        assert s.models["ollama"] == "llama3.2:3b" and s.models["groq"] == "groq-model"  # blank = .env default
        assert llm_client.active() == {"provider": "ollama", "label": "Ollama", "model": "llama3.2:3b"}
        assert ai_settings.reset().chain == ["groq", "gemini"]


def test_auto_model_uses_first_listed(keys):
    use("groq")
    with patch.object(Config, "GROQ_MODEL", ""), \
         patch.object(PROVIDERS["groq"], "list_models", return_value=["a-model", "b-model"]), \
         patch.object(PROVIDERS["groq"], "chat", return_value="ok") as chat:
        assert llm_client.complete("hi") == "ok"
    assert chat.call_args.args[1] == "a-model"


def test_connection_test_reports_provider_and_model(keys):
    with patch.object(PROVIDERS["gemini"], "chat", return_value="OK"):
        res = llm_client.test_connection("gemini")
    assert res["status"] == "SUCCESS" and res["model"] == "gemini-model"
    with patch.object(Config, "GROQ_API_KEY", ""):
        assert llm_client.test_connection("groq")["status"] == "NOT_CONFIGURED"
