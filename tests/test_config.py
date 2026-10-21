"""
Unit tests for Configuration module.
"""

from pathlib import Path
from app.config import Config


def test_config_defaults():
    assert Config.PROJECT_NAME == "IntelligentEmailCopilot"
    assert Config.EMBEDDING_MODEL == "all-MiniLM-L6-v2"
    assert Config.MAX_RETRIES == 2
    assert Path(Config.CHROMA_PERSIST_DIRECTORY).exists()
    assert Path(Config.KNOWLEDGE_BASE_PATH).exists()


def test_safe_dict_masks_secrets():
    safe_dict = Config.to_safe_dict()
    assert "GROQ_API_KEY" not in safe_dict and "GEMINI_API_KEY" not in safe_dict
    assert "GROQ_API_KEY_CONFIGURED" in safe_dict and "GEMINI_API_KEY_CONFIGURED" in safe_dict
    assert [k for k in safe_dict if k.endswith("_API_KEY_CONFIGURED")] == ["GROQ_API_KEY_CONFIGURED", "GEMINI_API_KEY_CONFIGURED"]
    assert "PROJECT_NAME" in safe_dict
    assert safe_dict["PROJECT_NAME"] == "IntelligentEmailCopilot"
