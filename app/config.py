"""
Centralized Configuration Module for Intelligent Email Copilot.
All environment variables and system paths are loaded and managed here.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Base directory of the repository
BASE_DIR = Path(__file__).resolve().parent.parent

# Load environment variables from .env if present
load_dotenv(BASE_DIR / ".env")


class Config:
    """Central configuration for Intelligent Email Copilot."""

    # Project Metadata
    PROJECT_NAME: str = os.getenv("PROJECT_NAME", "IntelligentEmailCopilot")
    COMPANY_NAME: str = os.getenv("COMPANY_NAME", "Support Team")

    # AI providers: ollama | groq | gemini.
    # LLM_PROVIDER answers first; the providers in LLM_FALLBACK_PROVIDERS are tried in order
    # when it fails. An administrator can change the provider order and each provider's model in
    # Settings -> Advanced; that choice is stored in the database and overrides these defaults.
    # A blank model means "the first chat model the provider lists".
    LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "ollama").strip().lower()
    LLM_FALLBACK_PROVIDERS: str = os.getenv("LLM_FALLBACK_PROVIDERS", "").strip().lower()
    LLM_TEMPERATURE: float = float(os.getenv("LLM_TEMPERATURE", "0.1"))
    LLM_MAX_TOKENS: int = int(os.getenv("LLM_MAX_TOKENS", "2000"))
    # Seconds before one AI request attempt is abandoned (a total deadline, not a socket timeout),
    # and extra attempts per provider for temporary failures (timeouts, rate limits, 5xx).
    LLM_TIMEOUT: float = float(os.getenv("LLM_TIMEOUT", "60"))
    LLM_RETRY_COUNT: int = max(0, int(os.getenv("LLM_RETRY_COUNT", "1")))
    # Longest pause (seconds) a rate-limited provider is waited for when a fallback could answer instead
    LLM_MAX_RETRY_WAIT: float = float(os.getenv("LLM_MAX_RETRY_WAIT", "5"))

    # Ollama (local, no key). An empty OLLAMA_BASE_URL disables it.
    OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").strip().rstrip("/")
    OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "").strip()
    # Local models are slower than hosted ones, especially on a CPU
    OLLAMA_TIMEOUT: float = float(os.getenv("OLLAMA_TIMEOUT", "300"))
    # Context window requested from Ollama (its own default can be far larger than the model supports)
    OLLAMA_NUM_CTX: int = int(os.getenv("OLLAMA_NUM_CTX", "16384"))
    # "false" turns off the reasoning phase of thinking models (much faster); "true" or "" = model default
    OLLAMA_THINK: str = os.getenv("OLLAMA_THINK", "false").strip().lower()

    # Groq (hosted, OpenAI-compatible API)
    GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "").strip()
    GROQ_BASE_URL: str = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1").strip().rstrip("/")
    GROQ_MODEL: str = os.getenv("GROQ_MODEL", "").strip()
    # Seconds per attempt (0 = LLM_TIMEOUT)
    GROQ_TIMEOUT: float = float(os.getenv("GROQ_TIMEOUT", "") or 0)
    # Reasoning effort for reasoning models (low | medium | high; "" = model default, recommended:
    # "low" made openai/gpt-oss-120b decline questions the knowledge answers).
    # Models without reasoning are called without it automatically.
    GROQ_REASONING_EFFORT: str = os.getenv("GROQ_REASONING_EFFORT", "").strip().lower()

    # Google Gemini (hosted)
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", os.getenv("GOOGLE_API_KEY", "")).strip()
    GEMINI_BASE_URL: str = os.getenv(
        "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"
    ).strip().rstrip("/")
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "").strip()
    GEMINI_TIMEOUT: float = float(os.getenv("GEMINI_TIMEOUT", "") or 0)  # 0 = LLM_TIMEOUT
    # Thinking level for models that think (minimal | low | medium | high; "" = model default)
    GEMINI_THINKING_LEVEL: str = os.getenv("GEMINI_THINKING_LEVEL", "").strip().lower()
    # Gemini counts thinking against the output limit, so this many tokens are added to it
    GEMINI_THINKING_TOKENS: int = int(os.getenv("GEMINI_THINKING_TOKENS", "2048"))

    # Local / Open-source Embeddings & ChromaDB
    # Changing the model rebuilds the index automatically. Chunks are sized to the model's
    # input window (256 tokens for all-MiniLM-L6-v2, 512 for e.g. BAAI/bge-small-en-v1.5).
    EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2").strip()
    # Optional instruction prepended to search queries (known models get the published one automatically)
    EMBEDDING_QUERY_INSTRUCTION: str = os.getenv("EMBEDDING_QUERY_INSTRUCTION", "")
    # Cosine similarity unrelated texts reach with the configured model; relevance is
    # rescaled from it so RETRIEVAL_MIN_RELEVANCE keeps its meaning if the model changes.
    # Empty = the measured value for known models (see rag/embeddings.py), else 0.
    EMBEDDING_SIMILARITY_FLOOR: str = os.getenv("EMBEDDING_SIMILARITY_FLOOR", "").strip()
    # Cross-encoder that re-scores search candidates against the question ("none" disables it;
    # if the model cannot be loaded the search falls back to hybrid ranking only).
    RERANKER_MODEL: str = os.getenv("RERANKER_MODEL", "cross-encoder/ms-marco-MiniLM-L6-v2").strip()
    CHUNK_SIZE: int = int(os.getenv("CHUNK_SIZE", "1000"))
    CHUNK_OVERLAP: int = int(os.getenv("CHUNK_OVERLAP", "150"))
    # Passages kept per search query, and in total for one answer
    RETRIEVAL_TOP_K: int = int(os.getenv("RETRIEVAL_TOP_K", "5"))
    RETRIEVAL_MAX_CHUNKS: int = int(os.getenv("RETRIEVAL_MAX_CHUNKS", "8"))
    # Upper bound for the knowledge text given to the AI for one answer (characters)
    RETRIEVAL_MAX_CONTEXT_CHARS: int = int(os.getenv("RETRIEVAL_MAX_CONTEXT_CHARS", "12000"))
    CHROMA_PERSIST_DIRECTORY: str = str(
        Path(os.getenv("CHROMA_PERSIST_DIRECTORY", str(BASE_DIR / "data" / "chroma"))).resolve()
    )
    KNOWLEDGE_BASE_PATH: str = str(
        Path(os.getenv("KNOWLEDGE_BASE_PATH", str(BASE_DIR / "data" / "documents"))).resolve()
    )

    # Workflow & Retry Limits
    MAX_RETRIES: int = int(os.getenv("MAX_RETRIES", "2"))
    MAX_EMAILS_PER_RUN: int = int(os.getenv("MAX_EMAILS_PER_RUN", "15"))
    # Emails answered in parallel during one inbox check
    EMAIL_CONCURRENCY: int = max(1, int(os.getenv("EMAIL_CONCURRENCY", "3")))

    # Gmail OAuth Paths
    GMAIL_CREDENTIALS_PATH: str = str(
        Path(os.getenv("GMAIL_CREDENTIALS_PATH", str(BASE_DIR / "credentials.json"))).resolve()
    )
    GMAIL_TOKEN_PATH: str = str(
        Path(os.getenv("GMAIL_TOKEN_PATH", str(BASE_DIR / "token.json"))).resolve()
    )
    MY_EMAIL: str = os.getenv("MY_EMAIL", "").strip().lower()

    # Web application
    PRODUCT_NAME: str = os.getenv("PRODUCT_NAME", "Intelligent Email Copilot").strip()
    DATA_DIR: str = str(Path(os.getenv("DATA_DIR", str(BASE_DIR / "data"))).resolve())
    APP_DB_PATH: str = str(
        Path(os.getenv("APP_DB_PATH", str(Path(DATA_DIR) / "copilot.db"))).resolve()
    )
    WORKSPACES_DIR: str = str(Path(DATA_DIR) / "workspaces")
    # Fernet key for encrypting stored OAuth tokens (auto-generated if blank)
    APP_SECRET_KEY: str = os.getenv("APP_SECRET_KEY", "").strip()
    # Optional admin token that unlocks Advanced Settings from non-local browsers
    ADMIN_TOKEN: str = os.getenv("ADMIN_TOKEN", "").strip()
    # Public URL of the web app, used to build the Google OAuth redirect URI
    PUBLIC_BASE_URL: str = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    MAX_UPLOAD_MB: int = int(os.getenv("MAX_UPLOAD_MB", "20"))
    # Minimum cosine similarity (0-1) for a knowledge chunk to be used in a reply
    RETRIEVAL_MIN_RELEVANCE: float = float(os.getenv("RETRIEVAL_MIN_RELEVANCE", "0.30"))

    @classmethod
    def ensure_directories(cls):
        """Ensures that required data directories exist."""
        Path(cls.CHROMA_PERSIST_DIRECTORY).mkdir(parents=True, exist_ok=True)
        Path(cls.KNOWLEDGE_BASE_PATH).mkdir(parents=True, exist_ok=True)
        Path(cls.WORKSPACES_DIR).mkdir(parents=True, exist_ok=True)

    @classmethod
    def is_gmail_configured(cls) -> bool:
        """Returns True if Gmail credentials or token exists."""
        return Path(cls.GMAIL_CREDENTIALS_PATH).exists() or Path(cls.GMAIL_TOKEN_PATH).exists()

    @classmethod
    def to_safe_dict(cls) -> dict:
        """Returns configuration summary without exposing sensitive secrets."""
        return {
            "PROJECT_NAME": cls.PROJECT_NAME,
            "COMPANY_NAME": cls.COMPANY_NAME,
            "LLM_PROVIDER": cls.LLM_PROVIDER,
            "LLM_FALLBACK_PROVIDERS": cls.LLM_FALLBACK_PROVIDERS or "none",
            "LLM_TEMPERATURE / MAX_TOKENS": f"{cls.LLM_TEMPERATURE} / {cls.LLM_MAX_TOKENS}",
            "LLM_TIMEOUT / RETRY_COUNT": f"{cls.LLM_TIMEOUT:g} / {cls.LLM_RETRY_COUNT}",
            "OLLAMA_BASE_URL": cls.OLLAMA_BASE_URL or "disabled",
            "OLLAMA_MODEL": cls.OLLAMA_MODEL or "auto",
            "GROQ_API_KEY_CONFIGURED": bool(cls.GROQ_API_KEY),
            "GROQ_MODEL": cls.GROQ_MODEL or "auto",
            "GEMINI_API_KEY_CONFIGURED": bool(cls.GEMINI_API_KEY),
            "GEMINI_MODEL": cls.GEMINI_MODEL or "auto",
            "EMBEDDING_MODEL": cls.EMBEDDING_MODEL,
            "RERANKER_MODEL": cls.RERANKER_MODEL or "none",
            "CHUNK_SIZE / OVERLAP": f"{cls.CHUNK_SIZE} / {cls.CHUNK_OVERLAP}",
            "RETRIEVAL_TOP_K / MAX_CHUNKS": f"{cls.RETRIEVAL_TOP_K} / {cls.RETRIEVAL_MAX_CHUNKS}",
            "RETRIEVAL_MIN_RELEVANCE": cls.RETRIEVAL_MIN_RELEVANCE,
            "EMAIL_CONCURRENCY": cls.EMAIL_CONCURRENCY,
            "CHROMA_PERSIST_DIRECTORY": cls.CHROMA_PERSIST_DIRECTORY,
            "KNOWLEDGE_BASE_PATH": cls.KNOWLEDGE_BASE_PATH,
            "MAX_RETRIES": cls.MAX_RETRIES,
            "MAX_EMAILS_PER_RUN": cls.MAX_EMAILS_PER_RUN,
            "GMAIL_CREDENTIALS_EXISTS": Path(cls.GMAIL_CREDENTIALS_PATH).exists(),
            "GMAIL_TOKEN_EXISTS": Path(cls.GMAIL_TOKEN_PATH).exists(),
            "MY_EMAIL": cls.MY_EMAIL if cls.MY_EMAIL else "Not set",
        }


# Automatically ensure storage directories exist
Config.ensure_directories()
