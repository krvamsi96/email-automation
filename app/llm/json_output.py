"""
JSON answers from the LLM, validated into Pydantic models.

The model is shown a concrete example object (not a JSON schema: given a schema,
the model tends to echo the schema back instead of filling it in, which would
silently turn into default values). The reply must contain every expected key;
otherwise the model is asked once more, and a failure is raised to the caller
instead of returning defaults.
"""

import json
import logging
import re
from typing import Any, Dict, Optional, Type, TypeVar

from pydantic import BaseModel

from app.llm.client import llm_client
from app.llm.providers import LLMError

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


def _parse(text: str) -> Optional[Dict[str, Any]]:
    clean = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", clean, re.DOTALL)
    if fence:
        clean = fence.group(1).strip()
    start, end = clean.find("{"), clean.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(clean[start:end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    # Schema echo ({"properties": {"field": {..., "value": x}}}) -> {"field": x}
    props = data.get("properties")
    if isinstance(props, dict) and all(isinstance(v, dict) for v in props.values()):
        data = {k: v["value"] for k, v in props.items() if "value" in v}
    return data


def complete_json(prompt: str, model: Type[T], example: Dict[str, Any], max_tokens: int = 600) -> T:
    """Asks for a JSON object shaped like `example` and returns it as `model`. Raises on failure."""
    keys = list(example)
    instruction = ("\n\nReply with ONLY a JSON object (no markdown, no explanation) with exactly these keys, "
                   f"for example:\n{json.dumps(example)}")
    reply = llm_client.complete(prompt + instruction, temperature=0.0, max_tokens=max_tokens)
    data = _parse(reply)
    if data is None or not all(k in data for k in keys):
        logger.warning("AI reply was not in the expected JSON format; asking again")
        retry = (f"{prompt}{instruction}\n\nYour previous reply was not in the required format. "
                 f"Return a JSON object with the keys {', '.join(keys)} and nothing else.")
        data = _parse(llm_client.complete(retry, temperature=0.0, max_tokens=max_tokens))
        if data is None or not all(k in data for k in keys):
            raise LLMError("The AI reply could not be read as the expected JSON object")
    return model.model_validate({k: data[k] for k in keys if data.get(k) is not None})
