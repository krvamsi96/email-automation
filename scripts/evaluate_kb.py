"""
End-to-end accuracy check of the live Knowledge Base with the real AI service:
Question -> retrieved excerpts -> context -> AI answer -> cited sources, plus follow-ups.
Runs the same LangGraph workflow as the Knowledge Base Test page (nothing is saved).

    python scripts/evaluate_kb.py

The cases below are written for the documents currently in data/documents (the
application itself contains no document-specific rules). When the Knowledge Base
changes, edit CASES: each expected string must appear in the answer, and None means
the answer must be "The requested information is not available in the Knowledge Base."
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.graph.knowledge_test import knowledge_test_graph  # noqa: E402

# (category, question, strings the answer must contain; None = must be "not available")
# Write these for the documents in your own Knowledge Base. The examples below only show the format.
CASES = [
    ("direct", "What should users do if they forget their password?", ["Forgot Password"]),
    ("exact-value", "What is the support helpline number?", ["<number from your documents>"]),
    ("request-vs-outcome", "Has the duplicate entry been deleted?", ["<wording of the reply>"]),
    ("not-in-KB", "What is the refund policy for cancelled flight tickets?", None),
    ("not-in-KB", "Who is the Prime Minister of India?", None),
    ("edge", "please check", None),
]
# (first question, follow-up question, strings the follow-up answer must contain)
FOLLOW_UPS = [
    ("What was the status of request 25 changed to?", "When was that done and by whom?", ["<date from your documents>"]),
]
NOT_AVAILABLE = "The requested information is not available in the Knowledge Base."


def run(question, history=None):
    state = {"workspace_id": "default", "question": question, "history": history or [], "search_question": "",
             "company_name": "Regulatory Compliance Portal team", "min_relevance": 0.30, "context": "", "sources": [],
             "evidence": [], "answer": "", "cited": [], "ai_error": "", "status": "", "message": ""}
    final = dict(state)
    trace = {}
    for step in knowledge_test_graph.stream(state):
        for node, update in step.items():
            final.update(update or {})
            if node == "retrieve_knowledge":
                trace = {"excerpts": list(update.get("evidence") or []), "context_chars": len(update.get("context") or "")}
    return final, trace


def show(category, question, final, trace, expect, elapsed):
    answer = final.get("answer") or ""
    if expect is None:
        ok = final["status"] == "no_knowledge" and answer == NOT_AVAILABLE
    else:
        flat = " ".join(answer.split()).lower()
        ok = final["status"] == "answered" and all(e.lower() in flat for e in expect)
    print(f"\n{'PASS' if ok else 'FAIL'} [{category}] {question}   ({elapsed:.1f}s)")
    if final.get("search_question") and final["search_question"] != question:
        print(f"   standalone question: {final['search_question']}")
    print(f"   retrieved: {len(trace.get('excerpts', []))} excerpts, context {trace.get('context_chars', 0)} chars")
    for n, e in enumerate(trace.get("excerpts", []), start=1):
        who = f" | {e.get('sender', '')} {e.get('date', '')}" if e.get("sender") else ""
        print(f"     [{n}] {e.get('role', ''):7} rel={e.get('relevance')} rr={e.get('rerank')} {e['document'][:48]}{who[:60]}")
    print(f"   status: {final['status']}   cited: {final.get('cited')}")
    print(f"   ANSWER: {answer}")
    print(f"   sources shown: {sorted({e['document'][:60] for e in final.get('evidence') or []})}")
    return ok


passed = total = 0
for category, question, expect in CASES:
    t = time.time()
    final, trace = run(question)
    passed += show(category, question, final, trace, expect, time.time() - t)
    total += 1
for first, follow, expect in FOLLOW_UPS:
    final1, _ = run(first)
    history = [{"question": first, "answer": final1.get("answer") if final1["status"] == "answered" else ""}]
    t = time.time()
    final, trace = run(follow, history)
    passed += show("follow-up", f"{follow}  (after: {first})", final, trace, expect, time.time() - t)
    total += 1
print(f"\n==== {passed}/{total} passed")
