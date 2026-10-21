"""
Retrieval service: the single knowledge-search entry point used by BOTH the
Knowledge Base Test and the Email Assistant, so both modes see identical results.

The retrieved passages are turned into numbered knowledge excerpts for the AI:
  * excerpts are grouped by document, documents in order of their best match;
  * consecutive passages of one message (or one document section) are joined
    into one excerpt, without the text the chunker repeated as overlap;
  * correspondence is shown in date order with its sender, date and subject,
    so the AI can tell a request from the reply that answered it;
  * the total size is capped (RETRIEVAL_MAX_CONTEXT_CHARS), best matches first.
The excerpt numbers in the context are the indexes of `evidence`, so the AI's
citations ([1], [2]) map straight back to the sources shown to the user.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.config import Config
from app.rag.correspondence import header_line
from app.rag.retriever import KnowledgeRetriever, get_retriever
from app.rag.vector_store import DEFAULT_WORKSPACE_ID

EXCERPT_CHARS = 700
MAX_OVERLAP = 400


@dataclass
class KnowledgeResult:
    context: str = ""
    sources: List[str] = field(default_factory=list)
    evidence: List[Dict] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return bool(self.evidence)


def _join(a: str, b: str) -> str:
    """Joins two consecutive chunks, dropping the overlap the chunker repeated at the start of b."""
    for size in range(min(len(a), len(b), MAX_OVERLAP), 19, -1):
        if a.endswith(b[:size]):
            return a + b[size:]
    return f"{a}\n{b}"


def _order_key(e: Dict[str, Any]):
    """Date order for messages (document order when no date is known), then chunk order."""
    return (e.get("timestamp") or "", e.get("message", -1), e.get("chunk") if e.get("chunk") is not None else 0)


def build_excerpts(evidence: List[Dict[str, Any]], max_chars: Optional[int] = None) -> List[Dict[str, Any]]:
    """Groups retrieved passages into the excerpts given to the AI (see module docstring)."""
    budget = max_chars or Config.RETRIEVAL_MAX_CONTEXT_CHARS
    selected: List[Dict[str, Any]] = []
    used = 0
    for e in evidence:  # matches first (best first), then surrounding messages
        if selected and used + len(e["text"]) > budget:
            continue
        selected.append(e)
        used += len(e["text"])

    doc_order: List[Any] = []
    for e in selected:
        key = e.get("doc_id") or e["document"]
        if key not in doc_order:
            doc_order.append(key)

    excerpts: List[Dict[str, Any]] = []
    for key in doc_order:
        items = sorted((e for e in selected if (e.get("doc_id") or e["document"]) == key), key=_order_key)
        for e in items:
            prev = excerpts[-1] if excerpts else None
            joinable = (prev is not None and (prev.get("doc_id") or prev["document"]) == key
                        and prev.get("message") == e.get("message")
                        and e.get("chunk") is not None and prev.get("_last_chunk") is not None
                        and e["chunk"] == prev["_last_chunk"] + 1)
            if joinable:
                prev["text"] = _join(prev["text"], e["text"])
                prev["_last_chunk"] = e["chunk"]
                for score in ("relevance", "rerank"):
                    if e.get(score) is not None:
                        prev[score] = max(prev.get(score) or 0.0, e[score])
                if e.get("role") == "match":
                    prev["role"] = "match"
                continue
            item = {k: v for k, v in e.items()}
            item["_last_chunk"] = e.get("chunk")
            excerpts.append(item)
    for x in excerpts:
        x.pop("_last_chunk", None)
    return excerpts


def format_context(excerpts: List[Dict]) -> str:
    """Numbered excerpts with their source (and sender/date for messages), as given to the AI."""
    blocks = []
    for n, e in enumerate(excerpts, start=1):
        where = e["document"] + (f", page {e['page']}" if e.get("page") else "")
        lines = [f"[{n}] Source: {where}"]
        message = header_line(e.get("sender", ""), e.get("date", ""), e.get("subject", ""))
        if message:
            lines.append(f"Message — {message}")
        lines.append(e["text"])
        blocks.append("\n".join(lines))
    return "\n\n---\n\n".join(blocks)


def find_knowledge(
    workspace_id: str,
    queries: List[str],
    min_relevance: Optional[float] = None,
    retriever: Optional[KnowledgeRetriever] = None,
) -> KnowledgeResult:
    """Searches a workspace's Knowledge Base. Passages below the match threshold are ignored."""
    retriever = retriever or get_retriever(workspace_id or DEFAULT_WORKSPACE_ID)
    excerpts = build_excerpts(retriever.retrieve_evidence(queries, min_relevance=min_relevance))

    sources: List[str] = []
    for e in excerpts:
        if e["document"] not in sources:
            sources.append(e["document"])
    public = [
        {**e, "text": e["text"][:EXCERPT_CHARS] + ("…" if len(e["text"]) > EXCERPT_CHARS else "")}
        for e in excerpts
    ]
    return KnowledgeResult(context=format_context(excerpts), sources=sources, evidence=public)


def cited_evidence(evidence: List[Dict], cited: List[int]) -> List[Dict]:
    """The excerpts an answer cited (1-based numbers), in citation order; all excerpts when none were cited."""
    picked = [evidence[n - 1] for n in cited if 1 <= n <= len(evidence)]
    return picked or list(evidence)
