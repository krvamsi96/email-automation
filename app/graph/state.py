"""
LangGraph State Definition for Intelligent Email Copilot.
Maintains state across all graph nodes for email processing.
"""

from typing import List, Dict, Any, Optional
from typing_extensions import TypedDict
from pydantic import BaseModel, Field


class EmailData(BaseModel):
    """Schema for individual email metadata and body."""
    id: str = Field(default="")
    threadId: str = Field(default="")
    messageId: str = Field(default="")
    references: str = Field(default="")
    sender: str = Field(default="")
    subject: str = Field(default="")
    body: str = Field(default="")
    snippet: str = Field(default="")


class CopilotState(TypedDict):
    """State dictionary passed between LangGraph nodes."""
    # Email queues
    emails_queue: List[Dict[str, Any]]
    current_email: Optional[Dict[str, Any]]

    # Screening & Classification
    is_suspicious: bool
    threat_type: str
    threat_reason: str
    email_category: str
    category_confidence: float

    # RAG Extraction & Retrieval
    primary_question: str
    rag_queries: List[str]
    retrieved_context: str
    has_sufficient_context: bool
    knowledge_answer: str

    # Response Generation & Validation
    generated_email: str
    is_valid: bool
    validation_score: int
    validation_feedback: str
    retry_count: int

    # Workspace / account context (web app); empty in CLI mode
    workspace_id: str
    account_id: str
    company_name: str
    tone: str
    signature: str
    draft_when_unknown: bool
    skip_promotions: bool
    min_relevance: float
    knowledge_sources: List[str]
    knowledge_evidence: List[Dict[str, Any]]
    ai_error: str
    draft_error: str
    draft_skipped_reason: str
    draft_message_id: str
    preview_only: bool

    # Draft & Metrics
    draft_id: Optional[str]
    processed_count: int
    flagged_count: int
    drafts_created_count: int
    skipped_count: int


def build_initial_state() -> CopilotState:
    """Builds a fresh, empty CopilotState for starting a graph execution run."""
    return {
        "emails_queue": [],
        "current_email": None,
        "is_suspicious": False,
        "threat_type": "none",
        "threat_reason": "",
        "email_category": "",
        "category_confidence": 1.0,
        "primary_question": "",
        "rag_queries": [],
        "retrieved_context": "",
        "has_sufficient_context": False,
        "knowledge_answer": "",
        "generated_email": "",
        "is_valid": False,
        "validation_score": 0,
        "validation_feedback": "",
        "retry_count": 0,
        "workspace_id": "",
        "account_id": "",
        "company_name": "",
        "tone": "professional",
        "signature": "",
        "draft_when_unknown": True,
        "skip_promotions": True,
        "min_relevance": -1.0,
        "knowledge_sources": [],
        "knowledge_evidence": [],
        "ai_error": "",
        "draft_error": "",
        "draft_skipped_reason": "",
        "draft_message_id": "",
        "preview_only": False,
        "draft_id": None,
        "processed_count": 0,
        "flagged_count": 0,
        "drafts_created_count": 0,
        "skipped_count": 0,
    }
