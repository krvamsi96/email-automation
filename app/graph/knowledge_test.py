"""
Knowledge Base Test workflow (no Gmail involved).

    START → receive_question → retrieve_knowledge ─┬─→ generate_answer → validate_answer → return_result → END
                                                   └─(nothing relevant)────────────────────→ return_result → END

Uses the same knowledge search (services/retrieval.py) and answer generation
(services/answers.py) as the email workflow. A follow-up question is first
rewritten into a standalone question using the earlier questions and answers.
"""

from typing import Any, Dict, List

from langgraph.graph import END, StateGraph
from typing_extensions import TypedDict

from app.agents.faq_agent import NOT_AVAILABLE, faq_agent, is_insufficient
from app.services.answers import answer_question
from app.services.retrieval import cited_evidence, find_knowledge


class KnowledgeTestState(TypedDict):
    workspace_id: str
    question: str
    history: List[Dict[str, str]]   # earlier questions/answers this one follows up on (oldest first)
    search_question: str            # the question as searched and answered (standalone)
    company_name: str
    min_relevance: float
    context: str
    sources: List[str]
    evidence: List[Dict[str, Any]]
    answer: str
    cited: List[int]
    ai_error: str
    status: str   # answered | no_knowledge | ai_unavailable
    message: str


def receive_question(state: KnowledgeTestState) -> Dict[str, Any]:
    question = " ".join((state.get("question") or "").split())
    history = state.get("history") or []
    search = faq_agent.condense_question(question, history) if history else question
    return {"question": question, "search_question": search}


def retrieve_knowledge(state: KnowledgeTestState) -> Dict[str, Any]:
    found = find_knowledge(state["workspace_id"], [state.get("search_question") or state["question"]],
                           state.get("min_relevance"))
    return {"context": found.context, "sources": found.sources, "evidence": found.evidence}


def route_after_retrieval(state: KnowledgeTestState) -> str:
    return "found" if state.get("evidence") else "nothing"


def generate(state: KnowledgeTestState) -> Dict[str, Any]:
    result = answer_question(state.get("search_question") or state["question"], state["context"],
                             state.get("company_name"))
    return {"answer": result.text, "ai_error": result.error, "cited": result.cited}


def validate_answer(state: KnowledgeTestState) -> Dict[str, Any]:
    answer = (state.get("answer") or "").strip()
    if state.get("ai_error"):
        return {"status": "ai_unavailable", "answer": "", "message": state["ai_error"]}
    if is_insufficient(answer):
        return {"status": "no_knowledge", "answer": ""}
    return {"status": "answered", "answer": answer}


def return_result(state: KnowledgeTestState) -> Dict[str, Any]:
    status = state.get("status") or "no_knowledge"
    if status == "no_knowledge":
        return {
            "status": status,
            "answer": NOT_AVAILABLE,
            "evidence": [],
            "message": "No relevant information was found in the current knowledge base. Please review this question manually.",
        }
    if status == "answered":
        # Only the excerpts the answer is based on (its citations) are reported as sources.
        evidence = cited_evidence(state.get("evidence") or [], state.get("cited") or [])
        sources: List[str] = []
        for e in evidence:
            if e["document"] not in sources:
                sources.append(e["document"])
        return {"message": "", "evidence": evidence, "sources": sources}
    return {}


def build():
    g = StateGraph(KnowledgeTestState)
    g.add_node("receive_question", receive_question)
    g.add_node("retrieve_knowledge", retrieve_knowledge)
    g.add_node("generate_answer", generate)
    g.add_node("validate_answer", validate_answer)
    g.add_node("return_result", return_result)
    g.set_entry_point("receive_question")
    g.add_edge("receive_question", "retrieve_knowledge")
    g.add_conditional_edges("retrieve_knowledge", route_after_retrieval,
                            {"found": "generate_answer", "nothing": "return_result"})
    g.add_edge("generate_answer", "validate_answer")
    g.add_edge("validate_answer", "return_result")
    g.add_edge("return_result", END)
    return g.compile()


knowledge_test_graph = build()
