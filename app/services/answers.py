"""
Response service: turns retrieved knowledge into an answer using the AI service.
Shared by the Knowledge Base Test and the Email Assistant (via the email workflow).
"""

from typing import Optional, Tuple

from app.agents.faq_agent import NO_ANSWER, GroundedAnswer, faq_agent
from app.llm.client import llm_client

AI_UNAVAILABLE = "AI service is temporarily unavailable. Please try again."


def answer_question(
    question: str, context: str, company_name: Optional[str] = None, message: Optional[str] = None,
) -> GroundedAnswer:
    """
    Grounded answer and the excerpt numbers it cites. `text` is NO_ANSWER when the
    knowledge does not answer the question; `error` is non-empty when the AI service failed.
    `message` is the full original email, when the question came from one.
    """
    if not context or not context.strip():
        return GroundedAnswer(NO_ANSWER)
    if not llm_client.is_configured():
        return GroundedAnswer(NO_ANSWER, AI_UNAVAILABLE)
    result = faq_agent.answer(question, context, company_name, message)
    if result.error:
        result.error = AI_UNAVAILABLE
    return result


def generate_answer(
    question: str, context: str, company_name: Optional[str] = None, message: Optional[str] = None,
) -> Tuple[str, str]:
    """(answer, error) view of answer_question."""
    result = answer_question(question, context, company_name, message)
    return result.text, result.error
