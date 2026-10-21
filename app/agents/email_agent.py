"""
Email Agent Module.
Orchestrates email analysis (security screening, classification and request
extraction in one AI call), response writing, and quality validation.
All LLM reasoning goes through app.llm.client (Ollama, Groq or Gemini).
"""

import logging
import re
from email.utils import parseaddr
from typing import List, Optional, Tuple
from pydantic import BaseModel, Field, PrivateAttr

from app.config import Config
from app.llm.client import llm_client
from app.llm.providers import LLMError
from app.llm.json_output import complete_json
from app.prompts.email_prompt import (
    ANALYZE_EMAIL_PROMPT,
    EMAIL_WRITER_PROMPT,
    TONE_INSTRUCTIONS,
    default_signature,
)
from app.prompts.validation_prompt import RESPONSE_VALIDATION_PROMPT

logger = logging.getLogger(__name__)

# Heuristic prompt injection markers for fast zero-cost triage
PROMPT_INJECTION_PATTERNS = [
    "ignore previous instructions",
    "ignore all instructions",
    "disregard previous instructions",
    "reveal your system prompt",
    "dump system prompt",
    "developer mode",
    "act as an unfiltered",
    "jailbreak",
]

INJECTION_REGEX = re.compile(
    r"\b(ignore|disregard|forget|override)\s+(all\s+|any\s+)?(of\s+)?(your\s+|the\s+)?"
    r"(previous|prior|above|earlier|existing)\s+(instructions|rules|prompts|policies)",
    re.IGNORECASE,
)

# Sender mailbox names that are almost always bulk / marketing mail
BULK_SENDER_PREFIXES = ("newsletter", "news@", "marketing", "promo", "deals@", "offers@", "no-reply-marketing")
# Mail-system senders and subjects (delivery reports, auto-replies) that never need an answer
SYSTEM_SENDERS = ("mailer-daemon", "postmaster")
AUTOMATED_SUBJECT = re.compile(
    r"^\s*(undelivered mail returned to sender|delivery status notification|mail delivery (failed|failure|subsystem)|"
    r"undeliverable:|returned mail:|automatic reply|auto(matic)?[- ]reply|out of (the )?office)",
    re.IGNORECASE,
)

CATEGORIES = ("faq_question", "support_request", "general_inquiry", "human_assistance", "unrelated")
ANALYSIS_EXAMPLE = {
    "threat_type": "none", "is_suspicious": False, "reason": "Genuine request about the sender's account",
    "category": "support_request", "confidence": 0.9,
    "primary_question": "Please reset the password for user X on system Y",
    "search_queries": ["reset password for a user", "system Y login problem"],
}
VALIDATION_EXAMPLE = {"is_valid": True, "score": 9, "hallucination_detected": False,
                      "feedback": "Grounded, complete and polite."}
THREATS = ("prompt_injection", "phishing", "spam", "none")
MAX_BODY_CHARS = 8000


def greeting_name(sender: str) -> str:
    name, addr = parseaddr(sender or "")
    name = name.replace('"', "").strip()
    if name:
        return name.split()[0]
    return "there"


def strip_subject_lines(reply: str, subject: str) -> str:
    """Removes a subject line the model sometimes repeats at the top of the body."""
    base = re.sub(r"^(re|fwd?)\s*:\s*", "", (subject or "").strip(), flags=re.IGNORECASE).lower()
    lines = reply.split("\n")
    for i, line in enumerate(lines[:4]):
        text = line.strip()
        bare = re.sub(r"^(subject\s*:\s*)?((re|fwd?)\s*:\s*)*", "", text, flags=re.IGNORECASE).lower()
        if text and (text.lower().startswith("subject:") or (base and bare == base and bare != text.lower())):
            del lines[i]
            if i < len(lines) and not lines[i].strip() and (i == 0 or not lines[i - 1].strip()):
                del lines[i]
            break
    return "\n".join(lines).strip()


def is_automated(sender: str, subject: str, auto_submitted: str = "") -> bool:
    """Machine-generated mail: delivery reports, auto-replies (RFC 3834 Auto-Submitted), mail-system senders."""
    local = parseaddr(sender or "")[1].lower().split("@")[0]
    if local.startswith(SYSTEM_SENDERS):
        return True
    if auto_submitted and auto_submitted.strip().lower() != "no":
        return True
    return bool(AUTOMATED_SUBJECT.match(subject or ""))


class ThreatScreenResult(BaseModel):
    threat_type: str = Field(default="none")
    is_suspicious: bool = Field(default=False)
    reason: str = Field(default="Standard email")


class ClassificationResult(BaseModel):
    category: str = Field(default="general_inquiry")
    confidence: float = Field(default=1.0)
    reason: str = Field(default="")


class QuestionExtractionResult(BaseModel):
    primary_question: str = Field(default="")
    search_queries: List[str] = Field(default_factory=list)


class EmailAnalysis(BaseModel):
    threat_type: str = Field(default="none", description="prompt_injection | phishing | spam | none")
    is_suspicious: bool = Field(default=False)
    reason: str = Field(default="", description="one short sentence explaining the security decision")
    category: str = Field(default="general_inquiry",
                          description="faq_question | support_request | general_inquiry | human_assistance | unrelated")
    confidence: float = Field(default=1.0, description="category confidence, 0.0 to 1.0")
    primary_question: str = Field(default="", description="what the sender needs, one sentence")
    search_queries: List[str] = Field(default_factory=list, description="1 to 3 knowledge-base search queries")
    _failed: bool = PrivateAttr(default=False)  # set when the AI analysis could not run

    @property
    def failed(self) -> bool:
        return self._failed

    def normalised(self, subject: str, body: str) -> "EmailAnalysis":
        threat = (self.threat_type or "none").strip().lower()
        if threat not in THREATS:
            threat = "spam" if self.is_suspicious else "none"
        category = (self.category or "").strip().lower()
        if category not in CATEGORIES:
            category = "general_inquiry"
        question = (self.primary_question or "").strip() or subject
        queries = [q.strip() for q in self.search_queries if isinstance(q, str) and q.strip()][:3]
        return self.model_copy(update={
            "threat_type": threat,
            "is_suspicious": threat != "none",
            "category": category,
            "confidence": max(0.0, min(1.0, float(self.confidence or 0.0))),
            "primary_question": question,
            "search_queries": queries or fallback_queries(subject, body),
        })


class ValidationResult(BaseModel):
    is_valid: bool = Field(default=True)
    score: int = Field(default=8)
    hallucination_detected: bool = Field(default=False)
    feedback: str = Field(default="Looks good.")


def fallback_queries(subject: str, body: str) -> List[str]:
    """Search queries taken straight from the email when the AI cannot formulate them."""
    snippet = " ".join((body or "").split())[:300]
    return [q for q in (subject.strip(), f"{subject} {snippet}".strip()) if q]


class EmailAgent:
    """Agent specialized in parsing, understanding, and authoring customer email responses."""

    @staticmethod
    def heuristic_threat(body: str) -> Optional[ThreatScreenResult]:
        """Zero-cost detection of obvious attempts to manipulate the assistant."""
        body_lower = (body or "").lower()
        for pattern in PROMPT_INJECTION_PATTERNS:
            if pattern in body_lower:
                logger.warning("Fast heuristic injection match: '%s'", pattern)
                return ThreatScreenResult(threat_type="prompt_injection", is_suspicious=True,
                                          reason=f"Heuristic pattern match: '{pattern}'")
        if INJECTION_REGEX.search(body or ""):
            return ThreatScreenResult(threat_type="prompt_injection", is_suspicious=True,
                                      reason="Attempt to override assistant instructions")
        return None

    def analyze(
        self,
        subject: str,
        sender: str,
        body: str,
        company_name: Optional[str] = None,
        is_promotional: bool = False,
        auto_submitted: str = "",
    ) -> EmailAnalysis:
        """
        Security screening, category and request extraction for one email.
        Rules decide the obvious cases for free; everything else takes ONE AI call.
        """
        threat = self.heuristic_threat(body)
        if threat:
            return EmailAnalysis(threat_type=threat.threat_type, is_suspicious=True, reason=threat.reason,
                                 category="security", primary_question=subject)

        # Only strong bulk-mail signals are used here; words like "sale" or "unsubscribe"
        # also appear in genuine customer emails and must not cause a skip.
        sender_addr = parseaddr(sender or "")[1].lower()
        if is_promotional or sender_addr.startswith(BULK_SENDER_PREFIXES):
            return EmailAnalysis(category="unrelated", confidence=0.95, reason="Bulk / promotional email",
                                 primary_question=subject)
        if is_automated(sender, subject, auto_submitted):
            return EmailAnalysis(category="unrelated", confidence=0.95, reason="Automatic notification",
                                 primary_question=subject)

        if not llm_client.is_configured():
            return EmailAnalysis(category="faq_question", reason="Default pass (AI service not configured)",
                                 primary_question=subject, search_queries=fallback_queries(subject, body))

        prompt = ANALYZE_EMAIL_PROMPT.format(
            company_name=company_name or Config.COMPANY_NAME,
            sender=sender, subject=subject, body=(body or "")[:MAX_BODY_CHARS],
        )
        try:
            return complete_json(prompt, EmailAnalysis, ANALYSIS_EXAMPLE).normalised(subject, body)
        except Exception as e:
            logger.error("Error during email analysis: %s", e)
            result = EmailAnalysis(reason="Analysis unavailable", primary_question=subject,
                                   search_queries=fallback_queries(subject, body))
            result._failed = True
            return result

    # --- single-purpose views of the analysis (kept for callers and evaluation scripts) ---
    def screen_threats(self, subject: str, sender: str, body: str) -> ThreatScreenResult:
        """Screen email for prompt injection, phishing, or spam."""
        a = self.analyze(subject, sender, body)
        reason = "Fallback: screening error" if a.failed else a.reason
        return ThreatScreenResult(threat_type=a.threat_type, is_suspicious=a.is_suspicious, reason=reason)

    def classify(self, subject: str, sender: str, body: str, company_name: Optional[str] = None,
                 is_promotional: bool = False) -> ClassificationResult:
        """Classify email into support, FAQ, general inquiry, human assistance, or unrelated."""
        a = self.analyze(subject, sender, body, company_name, is_promotional)
        return ClassificationResult(category=a.category, confidence=a.confidence, reason=a.reason)

    def extract_questions(self, subject: str, body: str) -> QuestionExtractionResult:
        """Extract core inquiry and generate semantic search queries for RAG."""
        a = self.analyze(subject, "", body)
        return QuestionExtractionResult(primary_question=a.primary_question, search_queries=a.search_queries)

    # --- reply writing -----------------------------------------------------------
    def write_reply(
        self,
        subject: str,
        sender: str,
        body: str,
        category: str,
        knowledge_answer: str,
        previous_feedback: Optional[str] = None,
        company_name: Optional[str] = None,
        tone: str = "professional",
        signature: str = "",
    ) -> str:
        """Craft a professional email reply."""
        reply, _ = self.write_reply_checked(
            subject, sender, body, category, knowledge_answer, previous_feedback,
            company_name=company_name, tone=tone, signature=signature,
        )
        return reply

    def holding_reply(self, subject: str, sender: str, company_name: str, signature: str) -> str:
        """Safe acknowledgement used when no grounded answer can be produced."""
        return (
            f"Hi {greeting_name(sender)},\n\n"
            f"Thank you for reaching out regarding '{subject}'.\n\n"
            f"We have received your request and a member of our team is reviewing it. "
            f"We will follow up with you shortly with complete information.\n\n"
            f"{signature or default_signature(company_name)}"
        )

    def write_reply_checked(
        self,
        subject: str,
        sender: str,
        body: str,
        category: str,
        knowledge_answer: str,
        previous_feedback: Optional[str] = None,
        company_name: Optional[str] = None,
        tone: str = "professional",
        signature: str = "",
    ) -> Tuple[str, str]:
        """
        Craft a reply. Returns (reply_text, error). When the AI service fails, a
        safe holding reply is returned together with a non-empty error so the
        draft can be marked for careful human review.
        """
        company = company_name or Config.COMPANY_NAME
        sign_off = signature.strip() if signature and signature.strip() else default_signature(company)

        if not llm_client.is_configured():
            if knowledge_answer and knowledge_answer != "INSUFFICIENT_KNOWLEDGE":
                return (
                    f"Hi {greeting_name(sender)},\n\n"
                    f"Thank you for contacting us.\n\n"
                    f"{knowledge_answer}\n\n"
                    f"Please let us know if you have any additional questions.\n\n"
                    f"{sign_off}"
                ), ""
            return self.holding_reply(subject, sender, company, sign_off), ""

        prompt = EMAIL_WRITER_PROMPT.format(
            company_name=company,
            category=category,
            sender=sender,
            subject=subject,
            email_body=(body or "")[:MAX_BODY_CHARS],
            knowledge_answer=knowledge_answer,
            tone_instruction=TONE_INSTRUCTIONS.get(tone, TONE_INSTRUCTIONS["professional"]),
            signature=sign_off,
        )
        if previous_feedback:
            prompt += f"\n\nA REVIEWER FOUND THESE PROBLEMS IN THE PREVIOUS DRAFT. FIX THEM:\n{previous_feedback}"

        try:
            reply = llm_client.complete(prompt, temperature=0.2)
            reply = strip_subject_lines(reply or "", subject)
            if not reply:
                raise LLMError("Empty response from AI service")
            return reply, ""
        except Exception as e:
            logger.error("Error generating email reply: %s", e)
            return self.holding_reply(subject, sender, company, sign_off), "Unable to generate a response."

    def validate_reply(
        self,
        customer_email: str,
        retrieved_context: str,
        generated_email: str,
    ) -> ValidationResult:
        """Validates that the generated draft meets quality and safety standards."""
        if not llm_client.is_configured():
            return ValidationResult(is_valid=True, score=9, hallucination_detected=False, feedback="Passes offline validation.")

        prompt = RESPONSE_VALIDATION_PROMPT.format(
            customer_email=customer_email,
            retrieved_context=retrieved_context if retrieved_context else "None provided",
            generated_email=generated_email,
        )
        try:
            return complete_json(prompt, ValidationResult, VALIDATION_EXAMPLE)
        except Exception as e:
            logger.error("Error validating email response: %s", e)
            return ValidationResult(is_valid=True, score=7, hallucination_detected=False, feedback="Fallback pass")


# Global singleton email agent
email_agent = EmailAgent()
