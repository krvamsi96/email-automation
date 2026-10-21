"""
Local and Open-Source Embeddings Module for Intelligent Email Copilot.
Runs 100% locally via SentenceTransformers. No external API call is made for
vector embeddings.

The model is loaded on first use (or by the startup warm-up), so importing this
module stays cheap. Encoding is serialised with a lock because the Hugging Face
fast tokenizer is not safe to call from several threads at once.
"""

import logging
import threading
from typing import List, Optional

from langchain_core.embeddings import Embeddings

from app.config import Config

logger = logging.getLogger(__name__)

# Query instructions published by the model authors for retrieval. Passages are
# embedded without an instruction. Models that are not listed need none.
QUERY_INSTRUCTIONS = {
    "bge-small-en-v1.5": "Represent this sentence for searching relevant passages: ",
    "bge-base-en-v1.5": "Represent this sentence for searching relevant passages: ",
    "bge-large-en-v1.5": "Represent this sentence for searching relevant passages: ",
}

# Similarity floor per model, measured on this project's knowledge base: relevance is
# rescaled from it so the match threshold (RETRIEVAL_MIN_RELEVANCE, default 0.30) admits
# passages from about the same point for each model (bge-small: raw cosine ~0.50, where
# related passages start; unrelated ones reach up to ~0.56 and are removed by re-ranking).
# Other models: set EMBEDDING_SIMILARITY_FLOOR after measuring.
SIMILARITY_FLOORS = {
    "all-minilm-l6-v2": 0.0,
    "bge-small-en-v1.5": 0.29,
}

DOCUMENT_BATCH = 64


def _model_key(model_name: str) -> str:
    return model_name.rsplit("/", 1)[-1].lower()


def default_query_instruction(model_name: str) -> str:
    return QUERY_INSTRUCTIONS.get(_model_key(model_name), "")


def similarity_floor(model_name: Optional[str] = None) -> float:
    """EMBEDDING_SIMILARITY_FLOOR when set, otherwise the known floor of the model (0 if unknown)."""
    if Config.EMBEDDING_SIMILARITY_FLOOR:
        return float(Config.EMBEDDING_SIMILARITY_FLOOR)
    return SIMILARITY_FLOORS.get(_model_key(model_name or Config.EMBEDDING_MODEL), 0.0)


class LocalSentenceEmbeddings(Embeddings):
    """
    Lightweight, high-performance local embeddings wrapper
    implementing the LangChain Embeddings interface.
    Vectors are L2-normalised, so a dot product equals cosine similarity.
    """

    def __init__(self, model_name: Optional[str] = None, query_instruction: Optional[str] = None):
        self.model_name = model_name or Config.EMBEDDING_MODEL
        if query_instruction is None:
            query_instruction = Config.EMBEDDING_QUERY_INSTRUCTION or default_query_instruction(self.model_name)
        self.query_instruction = query_instruction
        self._model = None
        self._load_lock = threading.Lock()
        self._encode_lock = threading.Lock()

    @property
    def model(self):
        if self._model is None:
            with self._load_lock:
                if self._model is None:
                    from sentence_transformers import SentenceTransformer

                    logger.info("Loading embedding model %s", self.model_name)
                    self._model = SentenceTransformer(self.model_name)
        return self._model

    def _encode(self, texts: List[str]) -> List[List[float]]:
        model = self.model  # triggers load if needed (guarded by _load_lock)
        with self._encode_lock:
            vectors = model.encode(
                texts, batch_size=64, normalize_embeddings=True,
                convert_to_numpy=True, show_progress_bar=False,
            )
        return vectors.tolist()

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """Embed passages. The model handles mini-batching internally via batch_size."""
        if not texts:
            return []
        return self._encode(texts)

    def embed_query(self, text: str) -> List[float]:
        return self._encode([self.query_instruction + text])[0]

    def embed_queries(self, texts: List[str]) -> List[List[float]]:
        """Embeds several queries in one pass."""
        if not texts:
            return []
        return self._encode([self.query_instruction + t for t in texts])

    @property
    def max_tokens(self) -> int:
        """Longest input (in model tokens) that is embedded; anything after it is silently ignored."""
        return int(getattr(self.model, "max_seq_length", 0) or 512)

    def count_tokens(self, text: str) -> int:
        """Number of model tokens for a text, including the special tokens added by the model."""
        model = self.model
        with self._encode_lock:  # the fast tokenizer is not thread-safe
            return len(model.tokenizer(text, add_special_tokens=True, truncation=False)["input_ids"])

    def warm_up(self) -> None:
        """Loads the model and runs one encode so the first real request is fast."""
        self.embed_query("warm up")


# Singleton embedding instance
_embeddings_instance: Optional[LocalSentenceEmbeddings] = None
_instance_lock = threading.Lock()


def get_embeddings() -> LocalSentenceEmbeddings:
    """Returns a singleton instance of the local embeddings model."""
    global _embeddings_instance
    if _embeddings_instance is None:
        with _instance_lock:
            if _embeddings_instance is None:
                _embeddings_instance = LocalSentenceEmbeddings()
    return _embeddings_instance
