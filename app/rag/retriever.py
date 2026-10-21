"""
RAG Retriever Module for Intelligent Email Copilot.

Hybrid search over a workspace's knowledge collection:
  * semantic search (local embeddings in ChromaDB) finds passages with the same meaning;
  * keyword search (BM25) finds passages sharing exact terms such as codes, IDs and names;
  * the two rankings are merged with reciprocal-rank fusion;
  * a cross-encoder re-scores the surviving candidates against the question (when available).

A passage is only used when it is relevant enough: its semantic match must reach
the workspace's match threshold, or it must contain nearly all of the question's
distinctive keywords (at least two of them). Unrelated emails therefore get no
"knowledge" and are routed to human review instead of being answered from
off-topic text.

For correspondence, a matching passage is expanded to its whole message and the
messages just before and after it in the same thread, so the AI sees a request
together with the reply that answered it (and the other way round).
"""

import logging
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from langchain_core.documents import Document

from app.config import Config
from app.rag.embeddings import get_embeddings, similarity_floor
from app.rag.ingestion import _shingles, chunk_heading, is_near_duplicate
from app.rag.lexical import BM25Index
from app.rag.reranker import get_reranker
from app.rag.vector_store import (
    DEFAULT_WORKSPACE_ID,
    VectorStoreManager,
    get_vector_store_manager,
)

logger = logging.getLogger(__name__)

RRF_K = 60                 # reciprocal-rank-fusion constant
CANDIDATE_POOL = 20        # candidates taken from each search before fusion
LEXICAL_MATCH = 0.8        # share of the query's keyword weight a passage must contain to count as a keyword match
LEXICAL_MIN_TERMS = 2      # ...and the number of distinct query terms it must contain ("please check" is not enough)
RERANK_POOL = 20           # candidates re-scored by the cross-encoder
RERANK_MIN = 0.02          # cross-encoder relevance (0-1) below which a candidate is not an answer to the question
EXPAND_HITS = 4            # best matches whose surrounding messages are added for context


def calibrated(similarity: float) -> float:
    """
    Maps raw cosine similarity onto a 0-1 relevance scale that means the same for
    every embedding model: models differ in the similarity unrelated texts get
    (their similarity floor), which is mapped to 0.
    """
    floor = similarity_floor()
    return max(0.0, min(1.0, (similarity - floor) / (1.0 - floor)))


@dataclass
class Hit:
    id: str
    text: str
    metadata: Dict[str, Any]
    relevance: float          # calibrated semantic relevance, 0-1
    score: float = 0.0        # fused ranking score
    rerank: Optional[float] = None   # cross-encoder relevance, 0-1 (None when re-ranking is unavailable)
    _sig: set = field(default=None, repr=False)

    @property
    def source(self) -> str:
        return self.metadata.get("source", "Knowledge Base")

    @property
    def signature(self) -> set:
        if self._sig is None:
            self._sig = _shingles(self.text)
        return self._sig

    def to_document(self) -> Document:
        return Document(page_content=self.text, metadata=dict(self.metadata))


def _without_duplicates(hits: List[Hit]) -> List[Hit]:
    """Drops passages that repeat a higher-ranked one (e.g. the same message quoted in two threads)."""
    kept: List[Hit] = []
    for hit in hits:
        if not any(is_near_duplicate(hit.signature, k.signature) for k in kept):
            kept.append(hit)
    return kept


def _evidence(meta: Dict[str, Any], text: str, relevance: Optional[float], role: str,
              rerank: Optional[float] = None) -> Dict[str, Any]:
    """One evidence item: the passage, where it comes from and why it was included."""
    item = {
        "document": meta.get("source", "Knowledge Base"),
        "doc_id": meta.get("doc_id"),
        "chunk": meta.get("chunk"),
        "text": text.strip(),
        "page": meta.get("page"),
        "relevance": None if relevance is None else round(relevance, 3),
        "role": role,  # "match": found by the search; "context": surrounding message of a match
    }
    if rerank is not None:
        item["rerank"] = round(rerank, 3)
    for key in ("message", "sender", "date", "subject", "timestamp"):
        if meta.get(key) not in (None, ""):
            item[key] = meta[key]
    return item


class KnowledgeRetriever:
    """Retrieves relevant context chunks from a workspace's vector store."""

    def __init__(
        self,
        top_k: Optional[int] = None,
        min_relevance: Optional[float] = None,
        manager: Optional[VectorStoreManager] = None,
    ):
        self.top_k = top_k or Config.RETRIEVAL_TOP_K
        self.min_relevance = Config.RETRIEVAL_MIN_RELEVANCE if min_relevance is None else min_relevance
        self.manager = manager or get_vector_store_manager(DEFAULT_WORKSPACE_ID)
        self._lexical: Optional[BM25Index] = None
        self._lexical_version = -1
        self._lock = threading.Lock()

    # --- keyword index ---------------------------------------------------
    def lexical_index(self) -> BM25Index:
        """BM25 index of the collection, rebuilt only after the collection changed."""
        with self._lock:
            if self._lexical is None or self._lexical_version != self.manager.version:
                snap = self.manager.snapshot()
                texts = [f"{chunk_heading(m or {})}\n{t or ''}" for t, m in zip(snap["texts"], snap["metadatas"])]
                self._lexical = BM25Index(snap["ids"], texts)
                self._lexical_version = snap["version"]
            return self._lexical

    # --- search ------------------------------------------------------------
    def _threshold(self, min_relevance: Optional[float]) -> float:
        return self.min_relevance if min_relevance is None or min_relevance < 0 else min_relevance

    def _rerank(self, query: str, hits: List[Hit]) -> List[Hit]:
        """
        Orders the best candidates by cross-encoder relevance and drops those that clearly
        do not answer the query. Without a re-ranker the hybrid order is kept.
        """
        head = hits[:RERANK_POOL]
        try:
            scores = get_reranker().scores(query, [f"{chunk_heading(h.metadata)}\n{h.text}" for h in head])
        except Exception as e:  # never let re-ranking break the search
            logger.warning("Re-ranking failed, using hybrid ranking: %s", e)
            scores = None
        if scores is None:
            return hits
        for hit, s in zip(head, scores):
            hit.rerank = s
        return sorted([h for h in head if h.rerank >= RERANK_MIN], key=lambda h: h.rerank, reverse=True)

    def search(
        self,
        query: str,
        top_k: Optional[int] = None,
        min_relevance: Optional[float] = None,
        embedding: Optional[List[float]] = None,
    ) -> List[Hit]:
        """Hybrid search for one query. Returns relevant, de-duplicated passages, best first."""
        if not query or not query.strip() or self.manager.count() == 0:
            return []
        k = top_k or self.top_k
        threshold = self._threshold(min_relevance)
        vector = np.asarray(embedding if embedding is not None else get_embeddings().embed_query(query))
        pool = max(CANDIDATE_POOL, k * 4)

        dense = self.manager.query(vector.tolist(), pool)
        rows = {d["id"]: d for d in dense}
        fused: Dict[str, float] = defaultdict(float)
        for rank, d in enumerate(dense):
            fused[d["id"]] += 1.0 / (RRF_K + rank + 1)
        coverage: Dict[str, Tuple[float, int]] = {}
        for rank, (chunk_id, _, cover, terms) in enumerate(self.lexical_index().search(query, pool)):
            fused[chunk_id] += 1.0 / (RRF_K + rank + 1)
            coverage[chunk_id] = (cover, terms)

        missing = [cid for cid in fused if cid not in rows]
        for row in self.manager.get(missing, with_embeddings=True):
            row["similarity"] = float(np.dot(vector, np.asarray(row["embedding"])))
            rows[row["id"]] = row

        hits: List[Hit] = []
        for chunk_id, score in sorted(fused.items(), key=lambda kv: kv[1], reverse=True):
            row = rows.get(chunk_id)
            if row is None:
                continue  # removed while searching
            relevance = calibrated(row["similarity"])
            cover, terms = coverage.get(chunk_id, (0.0, 0))
            keyword_match = cover >= LEXICAL_MATCH and terms >= LEXICAL_MIN_TERMS and relevance >= threshold / 2
            if relevance < threshold and not keyword_match:
                continue
            hits.append(Hit(chunk_id, row["text"].strip(), row["metadata"], relevance, score))
        return self._rerank(query, _without_duplicates(hits))[:k]

    def _merged_hits(self, queries: List[str], min_relevance: Optional[float]) -> List[Hit]:
        """Hits for several sub-queries merged by rank fusion (a passage found by several queries ranks higher)."""
        vectors = get_embeddings().embed_queries(queries)
        merged: Dict[str, List[Any]] = {}
        for query, vector in zip(queries, vectors):
            for rank, hit in enumerate(self.search(query, min_relevance=min_relevance, embedding=vector)):
                bonus = 1.0 / (RRF_K + rank + 1)
                if hit.id in merged:
                    entry = merged[hit.id]
                    entry[1] += bonus
                    entry[0].relevance = max(entry[0].relevance, hit.relevance)
                    if hit.rerank is not None:
                        entry[0].rerank = max(entry[0].rerank or 0.0, hit.rerank)
                else:
                    merged[hit.id] = [hit, bonus]
        return [h for h, _ in sorted(merged.values(), key=lambda e: e[1], reverse=True)]

    def _thread_context(self, hits: List[Hit]) -> List[Dict[str, Any]]:
        """
        For the best correspondence matches: the rest of the matched message and the
        messages immediately before and after it (by date) in the same document.
        """
        chosen = {h.id for h in hits}
        extra: List[Dict[str, Any]] = []
        by_doc: Dict[str, List[Dict[str, Any]]] = {}
        for hit in hits[:EXPAND_HITS]:
            doc_id, number = hit.metadata.get("doc_id"), hit.metadata.get("message")
            if doc_id is None or number is None:
                continue
            if doc_id not in by_doc:
                by_doc[doc_id] = self.manager.document_chunks(doc_id)
            chunks = by_doc[doc_id]
            order = sorted({c["metadata"].get("message") for c in chunks if c["metadata"].get("message") is not None},
                           key=lambda m: _message_sort_key(chunks, m))
            if number not in order:
                continue
            pos = order.index(number)
            wanted = {number} | {order[p] for p in (pos - 1, pos + 1) if 0 <= p < len(order)}
            for c in chunks:
                if c["metadata"].get("message") in wanted and c["id"] not in chosen:
                    chosen.add(c["id"])
                    extra.append(_evidence(c["metadata"], c["text"], None, "context"))
        return extra

    def retrieve_evidence(
        self,
        queries: List[str],
        min_relevance: Optional[float] = None,
        max_chunks: Optional[int] = None,
    ) -> List[Dict]:
        """
        Relevant passages for several sub-queries, merged by rank fusion, de-duplicated
        and capped, followed by the surrounding messages of correspondence matches.
        Each item: {"document", "text", "page", "relevance", "role", ...message metadata}.
        """
        unique: List[str] = []
        for q in queries:
            q = " ".join((q or "").split())
            if q and q.casefold() not in {u.casefold() for u in unique}:
                unique.append(q)
        if not unique or self.manager.count() == 0:
            return []
        limit = max_chunks or Config.RETRIEVAL_MAX_CHUNKS
        hits = _without_duplicates(self._merged_hits(unique, min_relevance))[:limit]
        evidence = [_evidence(h.metadata, h.text, h.relevance, "match", h.rerank) for h in hits]
        if hits:
            evidence += self._thread_context(hits)
        return evidence

    # --- compatibility helpers ------------------------------------------------
    def retrieve_with_scores(self, query: str, top_k: int = None) -> List[Tuple[Document, float]]:
        """(chunk, relevance) pairs without applying the relevance threshold."""
        try:
            return [(h.to_document(), h.relevance) for h in self.search(query, top_k, min_relevance=0.0)]
        except Exception as e:
            logger.error("Error retrieving documents from knowledge store: %s", e)
            return []

    def retrieve(self, query: str, top_k: int = None) -> List[Document]:
        """Retrieves chunks relevant to the query (above the relevance threshold)."""
        return [h.to_document() for h in self.search(query, top_k)]

    def retrieve_context(self, queries: List[str]) -> Tuple[str, List[str]]:
        """Formatted context block for several sub-queries and the list of source documents used."""
        from app.services.retrieval import find_knowledge

        found = find_knowledge(DEFAULT_WORKSPACE_ID, queries, retriever=self)
        return found.context, found.sources

    def retrieve_multi_query(self, queries: List[str]) -> str:
        context, _ = self.retrieve_context(queries)
        return context

    def has_knowledge(self) -> bool:
        """Returns True if the vector store contains any indexed documents."""
        return self.manager.count() > 0


def _message_sort_key(chunks: List[Dict[str, Any]], number: int) -> Tuple[str, int]:
    """Chronological position of a message: its timestamp when known, else its position in the document."""
    stamp = next((c["metadata"].get("timestamp") for c in chunks
                  if c["metadata"].get("message") == number and c["metadata"].get("timestamp")), "")
    return (stamp or "", number)


_retrievers: Dict[str, KnowledgeRetriever] = {}
_retrievers_lock = threading.Lock()


def get_retriever(workspace_id: str = DEFAULT_WORKSPACE_ID) -> KnowledgeRetriever:
    with _retrievers_lock:
        retriever = _retrievers.get(workspace_id)
        if retriever is None:
            retriever = KnowledgeRetriever(manager=get_vector_store_manager(workspace_id))
            _retrievers[workspace_id] = retriever
        return retriever


# Global singleton retriever for the default workspace
knowledge_retriever = get_retriever(DEFAULT_WORKSPACE_ID)
