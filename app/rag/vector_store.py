"""
ChromaDB Vector Store Management Module.
Provides centralized access to per-workspace Chroma collections with local persistence.

Every chunk carries a ``doc_id`` metadata field so a single document's knowledge
can be removed or re-indexed without touching the rest of the collection.

The collection records the embedding model, chunk settings and indexing version
that produced it. Vectors from different models are not comparable, so a changed
setting (or an improved pipeline) is detected at startup and the knowledge is rebuilt.
"""

import logging
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import chromadb

from app.config import Config
from app.rag.embeddings import get_embeddings

logger = logging.getLogger(__name__)

DEFAULT_WORKSPACE_ID = "default"
LEGACY_COLLECTION_NAME = "email_copilot_knowledge"
# Bump when text cleaning / chunking changes so existing knowledge is rebuilt.
# 3: correspondence split per message, chunks sized to the embedding model's input window.
INDEX_VERSION = 3

_clients: Dict[str, "chromadb.api.ClientAPI"] = {}
_clients_lock = threading.Lock()


def _get_client(persist_directory: str):
    """One PersistentClient per directory, shared by every manager in the process."""
    with _clients_lock:
        client = _clients.get(persist_directory)
        if client is None:
            Path(persist_directory).mkdir(parents=True, exist_ok=True)
            client = chromadb.PersistentClient(path=persist_directory)
            _clients[persist_directory] = client
        return client


def collection_name_for(workspace_id: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", workspace_id)
    return f"kb_{safe}"[:60]


def collection_metadata() -> Dict[str, Any]:
    """Settings that determine the stored vectors; knowledge built with other values is rebuilt."""
    return {"hnsw:space": "cosine", "embedding_model": Config.EMBEDDING_MODEL, "index_version": INDEX_VERSION,
            "chunk_size": Config.CHUNK_SIZE, "chunk_overlap": Config.CHUNK_OVERLAP}


INDEX_KEYS = ("embedding_model", "index_version", "chunk_size", "chunk_overlap")


class VectorStoreManager:
    """Manages one workspace's Chroma collection."""

    def __init__(self, persist_directory: Optional[str] = None, collection_name: Optional[str] = None):
        self.persist_directory = persist_directory or Config.CHROMA_PERSIST_DIRECTORY
        self.collection_name = collection_name or collection_name_for(DEFAULT_WORKSPACE_ID)
        self._lock = threading.RLock()
        self._collection = None
        # Incremented on every write; lets caches (keyword index) know when to rebuild.
        self.version = 0

    @property
    def embeddings(self):
        return get_embeddings()

    @property
    def client(self):
        return _get_client(self.persist_directory)

    @property
    def collection(self):
        with self._lock:
            if self._collection is None:
                self._collection = self.client.get_or_create_collection(
                    self.collection_name, metadata=collection_metadata(), embedding_function=None,
                )
            return self._collection

    def get_vector_store(self):
        """LangChain view of the same collection (for LangChain retrievers and tools)."""
        from langchain_chroma import Chroma

        return Chroma(client=self.client, collection_name=self.collection_name, embedding_function=self.embeddings)

    # --- index health ------------------------------------------------------
    def index_is_current(self) -> bool:
        """False when the stored vectors were made by another model, other chunk settings or an older pipeline."""
        meta = self.collection.metadata or {}
        expected = collection_metadata()
        return all(meta.get(key) == expected[key] for key in INDEX_KEYS)

    # --- writes ------------------------------------------------------------
    def add_chunks(
        self,
        ids: Sequence[str],
        texts: Sequence[str],
        metadatas: Sequence[Dict[str, Any]],
        embeddings: Sequence[Sequence[float]],
    ) -> int:
        if not ids:
            return 0
        with self._lock:
            self.collection.upsert(ids=list(ids), documents=list(texts), metadatas=list(metadatas),
                                   embeddings=[list(e) for e in embeddings])
            self.version += 1
        logger.info("Stored %d chunks in collection %s", len(ids), self.collection_name)
        return len(ids)

    def delete_document(self, doc_id: str) -> int:
        """Removes every chunk belonging to one document. Returns the number removed."""
        with self._lock:
            ids = self.collection.get(where={"doc_id": doc_id}, include=[]).get("ids", [])
            if ids:
                self.collection.delete(ids=ids)
                self.version += 1
        if ids:
            logger.info("Removed %d chunks for document %s from %s", len(ids), doc_id, self.collection_name)
        return len(ids)

    def reset(self):
        """Deletes the collection; it is recreated (with current metadata) on next use."""
        with self._lock:
            try:
                self.client.delete_collection(self.collection_name)
                logger.info("Chroma collection '%s' reset successfully.", self.collection_name)
            except Exception as e:
                logger.info("Collection %s did not need resetting: %s", self.collection_name, e)
            self._collection = None
            self.version += 1

    def drop_legacy_collection(self):
        """Removes the pre-workspace collection whose chunks were not tracked per document."""
        try:
            names = [c.name if hasattr(c, "name") else c for c in self.client.list_collections()]
            if LEGACY_COLLECTION_NAME in names:
                self.client.delete_collection(LEGACY_COLLECTION_NAME)
                logger.info("Removed legacy collection %s", LEGACY_COLLECTION_NAME)
        except Exception as e:
            logger.warning("Could not remove legacy collection: %s", e)

    # --- reads -------------------------------------------------------------
    def count_document(self, doc_id: str) -> int:
        return len(self.collection.get(where={"doc_id": doc_id}, include=[]).get("ids", []))

    def count(self) -> int:
        """Returns the number of chunks stored in the collection."""
        try:
            return self.collection.count()
        except Exception as e:
            logger.warning("Could not count documents in vector store: %s", e)
            return 0

    def query(self, embedding: Sequence[float], k: int) -> List[Dict[str, Any]]:
        """Nearest chunks to a query vector: [{id, text, metadata, similarity}], most similar first."""
        n = self.count()
        if n == 0:
            return []
        res = self.collection.query(query_embeddings=[list(embedding)], n_results=min(k, n),
                                    include=["documents", "metadatas", "distances"])
        return [
            {"id": i, "text": t or "", "metadata": m or {}, "similarity": 1.0 - float(d)}
            for i, t, m, d in zip(res["ids"][0], res["documents"][0], res["metadatas"][0], res["distances"][0])
        ]

    def get(self, ids: Sequence[str], with_embeddings: bool = False) -> List[Dict[str, Any]]:
        if not ids:
            return []
        include = ["documents", "metadatas"] + (["embeddings"] if with_embeddings else [])
        res = self.collection.get(ids=list(ids), include=include)
        embeddings = res.get("embeddings")
        return [
            {"id": i, "text": res["documents"][n] or "", "metadata": res["metadatas"][n] or {},
             "embedding": embeddings[n] if with_embeddings else None}
            for n, i in enumerate(res["ids"])
        ]

    def snapshot(self) -> Dict[str, Any]:
        """Every chunk's id, text and metadata (used to build the keyword index)."""
        with self._lock:
            version = self.version
            res = self.collection.get(include=["documents", "metadatas"])
        return {"version": version, "ids": res.get("ids", []), "texts": res.get("documents", []),
                "metadatas": res.get("metadatas", [])}

    def document_chunks(self, doc_id: str) -> List[Dict[str, Any]]:
        """A document's chunks in document order."""
        res = self.collection.get(where={"doc_id": doc_id}, include=["documents", "metadatas"])
        rows = [{"id": i, "text": t or "", "metadata": m or {}}
                for i, t, m in zip(res.get("ids", []), res.get("documents", []), res.get("metadatas", []))]
        return sorted(rows, key=lambda r: r["metadata"].get("chunk", 0))


_managers: Dict[str, VectorStoreManager] = {}
_managers_lock = threading.Lock()


def get_vector_store_manager(workspace_id: str = DEFAULT_WORKSPACE_ID) -> VectorStoreManager:
    with _managers_lock:
        manager = _managers.get(workspace_id)
        if manager is None:
            manager = VectorStoreManager(collection_name=collection_name_for(workspace_id))
            _managers[workspace_id] = manager
        return manager


# Global singleton manager for the default workspace (CLI and legacy callers)
vector_store_manager = get_vector_store_manager(DEFAULT_WORKSPACE_ID)
