"""
LangGraph Nodes Module for Intelligent Email Copilot.
Implements the core decision and action nodes for processing emails.
"""

import logging
from colorama import Fore, Style
from typing import Dict, Any

from app.config import Config
from app.graph.state import CopilotState
from app.agents.email_agent import email_agent
from app.agents.faq_agent import faq_agent
from app.llm.client import llm_client
from app.services.retrieval import cited_evidence

from app.rag.vector_store import DEFAULT_WORKSPACE_ID
from app.gmail.reader import gmail_reader, EmailMessage
from app.gmail.drafts import gmail_draft_manager
from app.prompts.email_prompt import default_signature

logger = logging.getLogger(__name__)


def _company(state: CopilotState) -> str:
    return state.get("company_name") or Config.COMPANY_NAME


class CopilotNodes:
    """Encapsulates all graph nodes and edge routing conditions."""

    def load_emails(self, state: CopilotState) -> Dict[str, Any]:
        """Loads emails from Gmail inbox if not already provided in state."""
        print(Fore.CYAN + "\n[INFO] Loading incoming emails..." + Style.RESET_ALL)
        if state.get("emails_queue"):
            print(Fore.GREEN + f"[INFO] Found {len(state['emails_queue'])} pre-queued email(s)." + Style.RESET_ALL)
            return {"emails_queue": state["emails_queue"]}

        fetched = gmail_reader.fetch_unanswered_emails(max_results=Config.MAX_EMAILS_PER_RUN)
        emails_queue = [e.to_dict() for e in fetched]
        print(Fore.GREEN + f"[INFO] Fetched {len(emails_queue)} unanswered email(s) from Gmail." + Style.RESET_ALL)
        return {"emails_queue": emails_queue}

    def check_queue(self, state: CopilotState) -> str:
        """Determines if there are emails remaining to process."""
        queue = state.get("emails_queue", [])
        if not queue:
            print(Fore.YELLOW + "[INFO] Email inbox queue is empty. Workflow complete." + Style.RESET_ALL)
            return "empty"
        return "has_emails"

    def pop_email(self, state: CopilotState) -> Dict[str, Any]:
        """Pops the next email from queue and resets per-email working state."""
        queue = list(state.get("emails_queue", []))
        current = queue.pop(0)
        print(Fore.BLUE + f"\n[INFO] Processing email: '{current.get('subject')}' from {current.get('sender')}" + Style.RESET_ALL)
        return {
            "emails_queue": queue,
            "current_email": current,
            "is_suspicious": False,
            "threat_type": "none",
            "threat_reason": "",
            "email_category": "",
            "primary_question": "",
            "rag_queries": [],
            "retrieved_context": "",
            "has_sufficient_context": False,
            "knowledge_answer": "",
            "knowledge_sources": [],
            "knowledge_evidence": [],
            "generated_email": "",
            "is_valid": False,
            "validation_score": 0,
            "validation_feedback": "",
            "retry_count": 0,
            "ai_error": "",
            "draft_error": "",
            "draft_skipped_reason": "",
            "draft_message_id": "",
            "draft_id": None,
        }

    def analyze_email(self, state: CopilotState) -> Dict[str, Any]:
        """Security screening, classification and request extraction (one AI call)."""
        email = state["current_email"]
        print(Fore.YELLOW + "[INFO] Analysing email (security, category, request)..." + Style.RESET_ALL)
        is_promotional = False
        if state.get("skip_promotions", True):
            is_promotional = bool(email.get("list_unsubscribe")) or bool(
                {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "SPAM"}.intersection(email.get("labels") or [])
            )
        result = email_agent.analyze(
            subject=email.get("subject", ""),
            sender=email.get("sender", ""),
            body=email.get("body", ""),
            company_name=_company(state),
            is_promotional=is_promotional,
            auto_submitted=email.get("auto_submitted", ""),
        )
        if result.is_suspicious:
            print(Fore.RED + f"[WARNING] Threat detected ({result.threat_type}): {result.reason}" + Style.RESET_ALL)
        else:
            print(Fore.MAGENTA + f"[INFO] Categorized as: '{result.category}' | Request: '{result.primary_question}'" + Style.RESET_ALL)

        update = {
            "is_suspicious": result.is_suspicious,
            "threat_type": result.threat_type,
            "threat_reason": result.reason,
            "email_category": result.category,
            "category_confidence": result.confidence,
            "primary_question": result.primary_question,
            "rag_queries": result.search_queries,
        }
        if result.failed:
            # Analysis could not run: continue, but make sure a person looks at it.
            update["ai_error"] = "Security screening could not be completed. Check this email carefully."
        return update

    def route_after_analysis(self, state: CopilotState) -> str:
        """Suspicious emails are flagged, unrelated ones skipped, everything else answered."""
        if state.get("is_suspicious"):
            return "suspicious"
        if state.get("email_category") == "unrelated":
            return "unrelated"
        return "process"

    def flag_suspicious(self, state: CopilotState) -> Dict[str, Any]:
        """Flags suspicious email and safely increments count without responding."""
        print(Fore.RED + "[INFO] Flagging email for security review. Skipping automatic response." + Style.RESET_ALL)
        return {
            "flagged_count": state.get("flagged_count", 0) + 1,
            "processed_count": state.get("processed_count", 0) + 1,
        }

    def skip_email(self, state: CopilotState) -> Dict[str, Any]:
        """Skips unrelated email without drafting a reply."""
        print(Fore.LIGHTBLACK_EX + "[INFO] Email is unrelated/newsletter. Skipping." + Style.RESET_ALL)
        return {
            "skipped_count": state.get("skipped_count", 0) + 1,
            "processed_count": state.get("processed_count", 0) + 1,
        }

    def retrieve_knowledge(self, state: CopilotState) -> Dict[str, Any]:
        """Queries the workspace knowledge store for relevant context."""
        queries = list(state.get("rag_queries") or [])
        if state.get("primary_question") and state["primary_question"] not in queries:
            queries.append(state["primary_question"])

        print(Fore.YELLOW + f"[INFO] RAG retrieval started (queries: {len(queries)})..." + Style.RESET_ALL)
        # Same knowledge search as the Knowledge Base Test.
        from app.services.retrieval import find_knowledge
        found = find_knowledge(state.get("workspace_id") or DEFAULT_WORKSPACE_ID, queries, state.get("min_relevance", -1.0))
        context, sources = found.context, found.sources

        has_context = bool(context and context.strip())
        if has_context:
            print(Fore.GREEN + f"[INFO] Relevant knowledge retrieved from {sources}." + Style.RESET_ALL)
        else:
            print(Fore.YELLOW + "[INFO] No direct matching context found in knowledge base." + Style.RESET_ALL)

        return {
            "retrieved_context": context,
            "has_sufficient_context": has_context,
            "knowledge_sources": sources,
            "knowledge_evidence": found.evidence,
        }

    def route_knowledge_status(self, state: CopilotState) -> str:
        """Routes based on whether relevant knowledge was retrieved."""
        return "has_context" if state.get("has_sufficient_context") else "no_context"

    def generate_rag_answer(self, state: CopilotState) -> Dict[str, Any]:
        """Generates factual answer using the retrieved knowledge."""
        print(Fore.YELLOW + "[INFO] Generating answer from retrieved knowledge..." + Style.RESET_ALL)
        email = state["current_email"]
        args = dict(
            question=state.get("primary_question", ""),
            context=state.get("retrieved_context", ""),
            company_name=_company(state),
            message=f"Subject: {email.get('subject', '')}\n\n{email.get('body', '')}",
        )
        cited: list = []
        if llm_client.is_configured():
            result = faq_agent.answer(**args)
            answer, error, cited = result.text, result.error, result.cited
        else:
            answer, error = faq_agent.generate_answer_checked(**args)
        update: Dict[str, Any] = {"knowledge_answer": answer}
        if error:
            update["ai_error"] = error
        if answer == "INSUFFICIENT_KNOWLEDGE" and not error:
            # The AI read the retrieved text and it did not answer the question.
            # (If the AI itself failed, keep the sources: they were genuinely found.)
            update["knowledge_sources"] = []
            update["knowledge_evidence"] = []
        elif cited:
            # Report the excerpts the answer is based on (the numbers it cited).
            evidence = cited_evidence(state.get("knowledge_evidence") or [], cited)
            update["knowledge_evidence"] = evidence
            update["knowledge_sources"] = list(dict.fromkeys(e["document"] for e in evidence))
        return update

    def handle_unknown_knowledge(self, state: CopilotState) -> Dict[str, Any]:
        """Sets safe human-review fallback when no knowledge exists."""
        print(Fore.YELLOW + "[INFO] Insufficient knowledge in database. Routing to human review draft." + Style.RESET_ALL)
        return {"knowledge_answer": "INSUFFICIENT_KNOWLEDGE"}

    def generate_email_draft(self, state: CopilotState) -> Dict[str, Any]:
        """Drafts the response email."""
        email = state["current_email"]
        retry_num = state.get("retry_count", 0)
        prefix = f" (Attempt {retry_num + 1})" if retry_num > 0 else ""
        print(Fore.YELLOW + f"[INFO] Writing email response draft{prefix}..." + Style.RESET_ALL)

        draft, error = email_agent.write_reply_checked(
            subject=email.get("subject", ""),
            sender=email.get("sender", ""),
            body=email.get("body", ""),
            category=state.get("email_category", "general_inquiry"),
            knowledge_answer=state.get("knowledge_answer", ""),
            previous_feedback=state.get("validation_feedback") if retry_num > 0 else None,
            company_name=_company(state),
            tone=state.get("tone") or "professional",
            signature=state.get("signature") or "",
        )
        # Keep an earlier failure (e.g. answer generation) even if writing succeeded.
        return {"generated_email": draft, "ai_error": error or state.get("ai_error", "")}

    def validate_response(self, state: CopilotState) -> Dict[str, Any]:
        """Validates draft quality, hallucination prevention, and relevance."""
        if (state.get("ai_error") or "").startswith("Unable to generate"):
            # A holding reply was used because generation failed; nothing to validate.
            return {"is_valid": True, "validation_feedback": "", "retry_count": state.get("retry_count", 0) + 1}

        print(Fore.YELLOW + "[INFO] Validating response quality and groundedness..." + Style.RESET_ALL)
        email = state["current_email"]
        validation = email_agent.validate_reply(
            customer_email=f"Subject: {email.get('subject')}\nBody: {email.get('body')}",
            retrieved_context=state.get("retrieved_context", ""),
            generated_email=state.get("generated_email", ""),
        )

        new_retry_count = state.get("retry_count", 0) + 1
        if validation.is_valid:
            print(Fore.GREEN + f"[INFO] Response validation passed! (Score: {validation.score}/10)" + Style.RESET_ALL)
        else:
            print(Fore.RED + f"[WARNING] Response validation failed: {validation.feedback}" + Style.RESET_ALL)

        return {
            "is_valid": validation.is_valid,
            "validation_score": validation.score,
            "validation_feedback": validation.feedback,
            "retry_count": new_retry_count,
        }

    def route_after_validation(self, state: CopilotState) -> str:
        """Determines whether to accept draft, retry generation, or escalate to human review."""
        if state.get("is_valid"):
            return "valid"
        if state.get("retry_count", 0) < Config.MAX_RETRIES:
            print(Fore.YELLOW + f"[INFO] Retrying generation ({state.get('retry_count')}/{Config.MAX_RETRIES})..." + Style.RESET_ALL)
            return "retry"
        print(Fore.RED + f"[WARNING] Max retries ({Config.MAX_RETRIES}) reached. Preparing human review draft." + Style.RESET_ALL)
        return "max_retries_exceeded"

    def prepare_escalation_draft(self, state: CopilotState) -> Dict[str, Any]:
        """Crafts a safe holding reply when automated validation failed repeatedly."""
        email = state["current_email"]
        company = _company(state)
        body = email_agent.holding_reply(
            subject=email.get("subject", ""),
            sender=email.get("sender", ""),
            company_name=company,
            signature=state.get("signature") or default_signature(company),
        )
        return {
            "generated_email": body,
            "is_valid": True,
            "ai_error": state.get("ai_error") or "The AI response did not pass quality checks; a holding reply was drafted instead.",
        }

    def create_gmail_draft(self, state: CopilotState) -> Dict[str, Any]:
        """
        Creates a Gmail draft reply.
        CRITICAL: Never automatically sends. Human review in Gmail is required.
        """
        if state.get("preview_only"):
            # Email Test mode: show the reply only; nothing is written to Gmail.
            return {"draft_id": None, "draft_skipped_reason": "preview",
                    "processed_count": state.get("processed_count", 0) + 1}
        if state.get("knowledge_answer") == "INSUFFICIENT_KNOWLEDGE" and not state.get("draft_when_unknown", True):
            print(Fore.YELLOW + "[INFO] No knowledge available and holding replies are disabled; leaving for a person to answer." + Style.RESET_ALL)
            return {
                "draft_id": None,
                "draft_skipped_reason": "no_knowledge",
                "processed_count": state.get("processed_count", 0) + 1,
            }

        email_dict = state["current_email"]
        original = EmailMessage(
            id=email_dict.get("id", ""),
            threadId=email_dict.get("threadId", ""),
            messageId=email_dict.get("messageId", ""),
            references=email_dict.get("references", ""),
            sender=email_dict.get("sender", ""),
            subject=email_dict.get("subject", ""),
            body=email_dict.get("body", ""),
        )

        account_id = state.get("account_id")
        draft = None
        draft_error = ""
        if account_id:
            # Web app: always use the account the email came from, never the CLI token.
            from app.services.errors import ServiceError
            from app.services.gmail_accounts import gmail_accounts
            try:
                service = gmail_accounts.get_service(account_id)
                draft = gmail_draft_manager.create_draft_reply(original, state.get("generated_email", ""), service=service)
            except ServiceError as e:
                draft_error = e.message
        else:
            draft = gmail_draft_manager.create_draft_reply(
                original_email=original,
                reply_content=state.get("generated_email", ""),
            )

        if draft and draft.get("id"):
            print(Fore.GREEN + Style.BRIGHT + f"[INFO] Gmail draft created (ID: {draft['id']}). Waiting for human approval; it will not be sent automatically." + Style.RESET_ALL)
            return {
                "draft_id": draft["id"],
                "draft_message_id": (draft.get("message") or {}).get("id", ""),
                "draft_error": "",
                "drafts_created_count": state.get("drafts_created_count", 0) + 1,
                "processed_count": state.get("processed_count", 0) + 1,
            }

        print(Fore.RED + "[ERROR] The Gmail draft could not be created." + Style.RESET_ALL)
        return {
            "draft_id": None,
            "draft_error": draft_error or "The draft could not be saved to Gmail.",
            "processed_count": state.get("processed_count", 0) + 1,
        }


# Global singleton nodes instance
nodes = CopilotNodes()
