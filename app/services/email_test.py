"""
Email Test: run a typed-in email (sender, subject, body) through the full email
workflow — security check, understanding, knowledge search, AI reply, quality
check — and show the result. No Gmail account is used and no draft is created.
"""

import logging
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict

from app.graph.state import build_initial_state
from app.services.errors import NotFound, ServiceError
from app.services.workspaces import workspace_service
from app.store.db import new_id, now_iso

logger = logging.getLogger(__name__)

# Node that just finished -> what happens next (shown while the test runs)
NEXT_STAGE = {
    "load_emails": "Checking security and understanding the email…",
    "pop_email": "Checking security and understanding the email…",
    "analyze_email": "Searching knowledge…",
    "retrieve_knowledge": "Writing AI reply…",
    "generate_rag_answer": "Writing AI reply…",
    "handle_unknown_knowledge": "Writing AI reply…",
    "generate_email_draft": "Checking reply quality…",
    "validate_response": "Finishing…",
}
# Node that finished -> steps shown as done
STEPS_OF = {
    "analyze_email": ("security", "understand"),
    "retrieve_knowledge": ("knowledge",), "generate_rag_answer": ("reply",), "generate_email_draft": ("reply",),
    "prepare_escalation_draft": ("reply",), "validate_response": ("quality",),
}


class EmailTestService:

    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="email-test")
        self._runs: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._lock = threading.Lock()

    def start(self, workspace_id: str, sender: Any, subject: Any, body: Any) -> Dict[str, Any]:
        sender = (str(sender or "").strip() or "customer@example.com")[:200]
        subject = str(subject or "").strip()[:300]
        body = str(body or "").strip()
        if not subject and not body:
            raise ServiceError("Enter a subject or a message body.")
        if len(body) > 20000:
            raise ServiceError("The message is too long.")
        run = {
            "id": new_id(), "workspace_id": workspace_id, "status": "processing",
            "stage": "Checking security and understanding the email…",
            "steps": [], "input": {"sender": sender, "subject": subject or "(No subject)", "body": body},
            "created_at": now_iso(),
        }
        with self._lock:
            self._runs[run["id"]] = run
            while len(self._runs) > 50:
                self._runs.popitem(last=False)
        self._executor.submit(self._run_safe, run)
        return self.get(workspace_id, run["id"])

    def get(self, workspace_id: str, run_id: str) -> Dict[str, Any]:
        with self._lock:
            run = self._runs.get(run_id)
            if not run or run["workspace_id"] != workspace_id:
                raise NotFound("This test was not found. Please run it again.")
            return {k: v for k, v in run.items() if k != "workspace_id"}

    def _update(self, run, **values):
        with self._lock:
            run.update(values)

    def _run_safe(self, run):
        try:
            self._run(run)
        except Exception:
            logger.exception("Email test failed")
            self._update(run, status="failed", stage="", message="Something went wrong. Please try again.")

    def _run(self, run):
        from app.graph.workflow import app as email_graph

        ws = run["workspace_id"]
        settings = workspace_service.settings(ws)
        state = build_initial_state()
        state.update({
            "emails_queue": [{
                "id": f"test-{run['id']}", "threadId": "", "messageId": "", "references": "",
                "sender": run["input"]["sender"], "subject": run["input"]["subject"], "body": run["input"]["body"],
            }],
            "workspace_id": ws,
            "company_name": settings["branding"]["company_name"],
            "tone": settings["ai"]["tone"],
            "signature": settings["ai"]["signature"],
            "draft_when_unknown": True,
            "skip_promotions": settings["email"]["skip_promotions"],
            "min_relevance": settings["advanced"]["min_relevance"],
            "preview_only": True,
        })
        final = dict(state)
        done = []
        for step in email_graph.stream(state, {"recursion_limit": 60}):
            for node, update in step.items():
                final.update(update or {})
                for step_name in STEPS_OF.get(node, ()):
                    if step_name not in done:
                        done.append(step_name)
                self._update(run, stage=NEXT_STAGE.get(node, run["stage"]), steps=list(done))

        if final.get("is_suspicious"):
            outcome = "flagged"
        elif final.get("email_category") == "unrelated":
            outcome = "skipped"
        elif (final.get("ai_error") or "").startswith("Unable to generate"):
            outcome = "ai_unavailable"
        elif final.get("knowledge_answer") == "INSUFFICIENT_KNOWLEDGE" and not final.get("knowledge_evidence"):
            outcome = "no_knowledge"
        else:
            outcome = "reply_ready"

        evidence = final.get("knowledge_evidence") or []
        sources = []
        for e in evidence:
            entry = next((s for s in sources if s["document"] == e["document"]), None)
            if not entry:
                entry = {"document": e["document"], "excerpts": []}
                sources.append(entry)
            entry["excerpts"].append({"text": e["text"], "page": e.get("page")})

        self._update(
            run, status=outcome, stage="", steps=list(done),
            output={
                "security": {
                    "safe": not final.get("is_suspicious"),
                    "reason": final.get("threat_reason", "") if final.get("is_suspicious") else "",
                },
                "category": final.get("email_category", ""),
                "question": final.get("primary_question", ""),
                "sources": sources,
                "reply": final.get("generated_email", "") if outcome not in ("flagged", "skipped") else "",
                "quality": {
                    "score": final.get("validation_score") or None,
                    "passed": bool(final.get("is_valid")),
                },
                "note": final.get("ai_error") or "",
            },
            completed_at=now_iso(),
        )


email_test_service = EmailTestService()
