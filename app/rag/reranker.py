"""
Cross-encoder re-ranking of knowledge search candidates.

The embedding search compares a question and a passage as two separate vectors;
a cross-encoder reads the question and the passage together and judges whether
the passage answers it. Re-scoring the few candidates that passed the hybrid
search therefore puts the passage that actually contains the answer first, and
lets clearly non-answering passages be dropped before they reach the AI.

The model runs locally (CPU is fine for a few dozen pairs). It is optional: when
RERANKER_MODEL is "none" or the model cannot be loaded (e.g. offline on first
use), search falls back to the hybrid ranking alone.
"""

import logging
import math
import threading
from typing import List, Optional, Sequence

from app.config import Config

logger = logging.getLogger(__name__)

DISABLED = {"", "none", "off", "false", "0"}


class Reranker:
    def __init__(self, model_name: Optional[str] = None):
        self.model_name = Config.RERANKER_MODEL if model_name is None else model_name
        self._model = None
        self._failed = self.model_name.lower() in DISABLED
        self._load_lock = threading.Lock()
        self._predict_lock = threading.Lock()

    @property
    def available(self) -> bool:
        return self._get_model() is not None

    def _get_model(self):
        if self._model is None and not self._failed:
            with self._load_lock:
                if self._model is None and not self._failed:
                    try:
                        from sentence_transformers import CrossEncoder

                        logger.info("Loading re-ranking model %s", self.model_name)
                        self._model = CrossEncoder(self.model_name)
                    except Exception as e:  # offline, missing model, incompatible version
                        logger.warning("Re-ranking disabled (%s could not be loaded: %s)", self.model_name, e)
                        self._failed = True
        return self._model

    def scores(self, query: str, passages: Sequence[str]) -> Optional[List[float]]:
        """Relevance of each passage to the query (0-1), or None when re-ranking is unavailable."""
        model = self._get_model()
        if model is None or not passages:
            return None if model is None else []
        with self._predict_lock:  # the tokenizer is not thread-safe
            logits = model.predict([(query, p) for p in passages], show_progress_bar=False)
        return [1.0 / (1.0 + math.exp(-float(x))) for x in logits]

    def warm_up(self) -> None:
        self.scores("warm up", ["warm up"])


_instance: Optional[Reranker] = None
_instance_lock = threading.Lock()


def get_reranker() -> Reranker:
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = Reranker()
    return _instance
