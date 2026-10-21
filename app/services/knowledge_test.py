"""
Knowledge Base Test service: ask a question against the Knowledge Base without Gmail.

Questions run in the background (the AI service can be slow); the browser polls
the result. Every question and its outcome is kept as test history.
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List

from app.services.errors import NotFound, ServiceError
from app.services.knowledge import knowledge_service
from app.services.workspaces import workspace_service
from app.store.db import dumps, get_db, loads, new_id, now_iso

logger = logging.getLogger(__name__)

STAGES = {
    "receive_question": "Searching knowledge…",
    "retrieve_knowledge": "Generating answer…",
    "generate_answer": "Checking answer…",
    "validate_answer": "Finishing…",
}
COLUMNS = ("id, question, status, stage, answer, evidence, message, created_at, completed_at, "
           "follow_up_to, search_question")
MAX_FOLLOW_UP_DEPTH = 3


def present(row: Dict[str, Any]) -> Dict[str, Any]:
    evidence = loads(row.get("evidence"), []) or []
    sources: List[Dict[str, Any]] = []
    for e in evidence:
        entry = next((s for s in sources if s["document"] == e["document"]), None)
        if not entry:
            entry = {"document": e["document"], "excerpts": []}
            sources.append(entry)
        entry["excerpts"].append({"text": e["text"], "page": e.get("page"), "relevance": e.get("relevance")})
    return {
        "id": row["id"],
        "question": row["question"],
        "follow_up_to": row.get("follow_up_to") or "",
        # The standalone question that was searched, when a follow-up had to be rewritten
        "search_question": row.get("search_question") or "",
        "answer": row["answer"] or "",
        "sources": sources,
        "status": row["status"],
        "stage": row["stage"] or "",
        "message": row["message"] or "",
        "created_at": row["created_at"],
        "completed_at": row["completed_at"],
    }


class KnowledgeTestService:

    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="knowledge-test")

    def ask(self, workspace_id: str, question: Any, follow_up_to: Any = None) -> Dict[str, Any]:
        question = " ".join(str(question or "").split())
        follow_up_to = str(follow_up_to or "").strip()
        if not question:
            raise ServiceError("Please type a question.")
        if len(question) > 1000:
            raise ServiceError("The question is too long (1,000 characters maximum).")
        stats = knowledge_service.stats(workspace_id)
        if stats["ready"] == 0:
            raise ServiceError(
                "Add at least one document to the Knowledge Base and wait until it is Ready before asking a question."
                if stats["processing"] == 0 else "Your documents are still being processed. Please wait until they are Ready.",
                code="no_knowledge_ready",
            )
        if follow_up_to:
            self.get(workspace_id, follow_up_to)  # NotFound when it is not a test of this workspace
        test_id = new_id()
        get_db().insert("knowledge_tests", {
            "id": test_id, "workspace_id": workspace_id, "question": question, "follow_up_to": follow_up_to,
            "status": "processing", "stage": "Searching knowledge…", "created_at": now_iso(),
        })
        self._executor.submit(self._run_safe, workspace_id, test_id, question)
        return self.get(workspace_id, test_id)

    def _run_safe(self, workspace_id: str, test_id: str, question: str) -> None:
        try:
            self._run(workspace_id, test_id, question)
        except Exception:
            logger.exception("Knowledge test %s failed", test_id)
            get_db().update("knowledge_tests", test_id, {
                "status": "failed", "stage": "", "completed_at": now_iso(),
                "message": "Something went wrong while answering. Please try again.",
            })

    def _history(self, workspace_id: str, test_id: str) -> List[Dict[str, str]]:
        """Earlier questions and answers a test follows up on, oldest first."""
        history: List[Dict[str, str]] = []
        row = get_db().fetchone("SELECT follow_up_to FROM knowledge_tests WHERE id = ?", (test_id,))
        parent = (row or {}).get("follow_up_to") or ""
        while parent and len(history) < MAX_FOLLOW_UP_DEPTH:
            prev = get_db().fetchone(
                "SELECT question, search_question, answer, status, follow_up_to FROM knowledge_tests "
                "WHERE id = ? AND workspace_id = ?", (parent, workspace_id))
            if not prev:
                break
            history.insert(0, {"question": prev["search_question"] or prev["question"],
                               "answer": prev["answer"] if prev["status"] == "answered" else ""})
            parent = prev["follow_up_to"] or ""
        return history

    def _run(self, workspace_id: str, test_id: str, question: str) -> None:
        from app.graph.knowledge_test import knowledge_test_graph

        settings = workspace_service.settings(workspace_id)
        history = self._history(workspace_id, test_id)
        state = {
            "workspace_id": workspace_id, "question": question, "history": history, "search_question": "",
            "cited": [],
            "company_name": settings["branding"]["company_name"],
            "min_relevance": settings["advanced"]["min_relevance"],
            "context": "", "sources": [], "evidence": [], "answer": "", "ai_error": "", "status": "", "message": "",
        }
        final = dict(state)
        for step in knowledge_test_graph.stream(state):
            for node, update in step.items():
                final.update(update or {})
                if node in STAGES:
                    stage = STAGES[node]
                    if node == "retrieve_knowledge" and not final.get("evidence"):
                        stage = "Finishing…"
                    get_db().update("knowledge_tests", test_id, {"stage": stage})

        # Excerpts are kept even when the AI was unavailable, so users see what was found.
        get_db().update("knowledge_tests", test_id, {
            "status": final.get("status") or "no_knowledge",
            "answer": final.get("answer") or "",
            "search_question": final.get("search_question") if history else "",
            "evidence": dumps(final.get("evidence") if final.get("status") != "no_knowledge" else []),
            "message": final.get("message") or "",
            "stage": "",
            "completed_at": now_iso(),
        })

    def get(self, workspace_id: str, test_id: str) -> Dict[str, Any]:
        row = get_db().fetchone(
            f"SELECT {COLUMNS} FROM knowledge_tests WHERE id = ? AND workspace_id = ?", (test_id, workspace_id))
        if not row:
            raise NotFound("This test result was not found.")
        return present(row)

    def history(self, workspace_id: str, limit: int = 30) -> List[Dict[str, Any]]:
        rows = get_db().fetchall(
            f"SELECT {COLUMNS} FROM knowledge_tests WHERE workspace_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (workspace_id, max(1, min(limit, 100))))
        return [present(r) for r in rows]

    def clear_history(self, workspace_id: str) -> int:
        return get_db().execute(
            "DELETE FROM knowledge_tests WHERE workspace_id = ? AND status != 'processing'", (workspace_id,))

    @staticmethod
    def recover_interrupted() -> None:
        get_db().execute(
            "UPDATE knowledge_tests SET status = 'failed', stage = '', "
            "message = 'The test was interrupted. Please ask again.' WHERE status = 'processing'")


knowledge_test_service = KnowledgeTestService()
