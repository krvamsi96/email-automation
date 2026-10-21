"""
FAQ & Knowledge Base Agent Module.
Answers questions grounded strictly in retrieved documents via the configured AI provider.

Grounding is enforced in three ways:
  * the rules are sent as a system message, apart from the (untrusted) excerpts;
  * the answer must cite the numbered excerpts it uses, so the sources shown to
    the user are the ones the answer actually came from;
  * exact values in the answer (numbers, IDs, codes, e-mail addresses, links) are
    checked against the excerpts; an answer with values that occur nowhere in the
    knowledge is regenerated once and otherwise withheld.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from app.config import Config
from app.llm.client import llm_client
from app.prompts.email_prompt import (
    CONDENSE_QUESTION_PROMPT,
    GROUNDING_RETRY_BLOCK,
    ORIGINAL_MESSAGE_BLOCK,
    RAG_ANSWER_GENERATION_PROMPT,
    RAG_ANSWER_SYSTEM_PROMPT,
)

logger = logging.getLogger(__name__)

NO_ANSWER = "INSUFFICIENT_KNOWLEDGE"
# Shown to people when the Knowledge Base does not contain the answer.
NOT_AVAILABLE = "The requested information is not available in the Knowledge Base."
MAX_MESSAGE_CHARS = 4000

# An answer that *opens* with one of these declined to answer, even without the marker.
# (A partial answer that mentions a gap later on is still an answer.)
REFUSAL = re.compile(
    r"^\W*(i (do not|don't|cannot|can't) (know|have|find|answer)|i'm (sorry|unable)|i am (sorry|unable)|"
    r"(sorry|unfortunately),? (i|there|the)|there is no (information|mention)|no (relevant )?information (is )?(available|provided|found)|"
    r"(this|the) (information|answer) is not (available|provided|mentioned|included)|"
    r"the requested information is not available|"
    r"(it is|it's) not (possible|clear) to (answer|determine)|cannot (be )?(answered|determined))",
    re.IGNORECASE,
)

# [1], [1, 2], [1-3]; some models (e.g. gpt-oss) write 【1】 or ［1］ instead
CITATION = re.compile(r"\s*[\[【［〔](\d+(?:\s*[,;-]\s*\d+)*)[\]】］〕]")
EMAIL = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
URL = re.compile(r"(?:https?://|www\.)[^\s<>()\"']+", re.IGNORECASE)
# Values that must be copied, not invented: codes mixing letters and digits, numbers of 3+ digits
CODE = re.compile(r"(?<![\w#])(?=[\w#/.-]*\d)[\w#][\w#/.-]*[\w#]|\b\d{3,}\b")


def is_insufficient(answer: Optional[str]) -> bool:
    text = (answer or "").strip()
    return not text or NO_ANSWER in text.upper() or bool(REFUSAL.search(text))


def _alnum(text: str) -> str:
    return re.sub(r"[\W_]+", "", (text or "").lower())


def unsupported_values(answer: str, *sources: str) -> List[str]:
    """Exact values (e-mails, links, codes, long numbers) in the answer that occur in none of the sources."""
    haystack = _alnum(" ".join(sources))
    values: List[str] = []
    text = CITATION.sub(" ", answer or "")
    for pattern in (EMAIL, URL):
        for m in pattern.finditer(text):
            values.append(m.group(0).rstrip(".,;:)/"))
        text = pattern.sub(" ", text)
    for m in CODE.finditer(text):
        value = m.group(0).strip(".-/")
        if re.search(r"\d", value) and (len(re.sub(r"\D", "", value)) >= 3 or re.search(r"[^\W\d_]", value)):
            values.append(value)
    missing = []
    for value in values:
        key = _alnum(value.replace(",", ""))
        if key and key not in haystack and value not in missing:
            missing.append(value)
    return missing


def citations(answer: str) -> Tuple[str, List[int]]:
    """(answer without [n] markers, cited excerpt numbers in order of first use)."""
    cited: List[int] = []
    for group in CITATION.findall(answer or ""):
        for part in re.split(r"\s*[,;]\s*", group):
            if "-" in part:
                lo, hi = (int(x) for x in part.split("-", 1))
                numbers = range(lo, hi + 1) if 0 < hi - lo < 20 else [lo, hi]
            else:
                numbers = [int(part)]
            for n in numbers:
                if n not in cited:
                    cited.append(n)
    clean = CITATION.sub("", answer or "")
    # "(see [7])" leaves "(see)" behind once the marker is removed
    clean = re.sub(r"[ \t]*\((?:see|source|sources|ref\.?)?[ \t]*\)", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"[ \t]+([.,;:!?])", r"\1", clean)
    return re.sub(r"[ \t]{2,}", " ", clean).strip(), cited


@dataclass
class GroundedAnswer:
    text: str                                  # NO_ANSWER when the knowledge does not answer the question
    error: str = ""                            # non-empty when the AI service failed
    cited: List[int] = field(default_factory=list)   # excerpt numbers the answer is based on

    @property
    def found(self) -> bool:
        return not self.error and self.text != NO_ANSWER


class FAQAgent:
    """Specialized agent for answering questions strictly from retrieved context."""

    def _prompt(self, question: str, context: str, message: Optional[str]) -> str:
        request = ""
        if message and message.strip():
            request = ORIGINAL_MESSAGE_BLOCK.format(message=message.strip()[:MAX_MESSAGE_CHARS])
        return RAG_ANSWER_GENERATION_PROMPT.format(question=question, context=context, request=request)

    def answer(
        self, question: str, context: str, company_name: Optional[str] = None, message: Optional[str] = None,
    ) -> GroundedAnswer:
        """Grounded answer with the excerpts it cites. Never raises: AI failures are reported in `error`."""
        if not context or not context.strip():
            return GroundedAnswer(NO_ANSWER)
        if not llm_client.is_configured():
            return GroundedAnswer(NO_ANSWER, "AI service is not configured.")
        system = RAG_ANSWER_SYSTEM_PROMPT.format(company_name=company_name or Config.COMPANY_NAME)
        prompt = self._prompt(question, context, message)
        sources = (context, question, message or "")
        try:
            raw = llm_client.complete(prompt, system_prompt=system, temperature=0.0)
            if not is_insufficient(raw):
                missing = unsupported_values(raw, *sources)
                if missing:
                    logger.warning("Answer used values not found in the knowledge %s; regenerating", missing)
                    retry = prompt + GROUNDING_RETRY_BLOCK.format(values=", ".join(missing))
                    raw = llm_client.complete(retry, system_prompt=system, temperature=0.0)
                    if not is_insufficient(raw) and unsupported_values(raw, *sources):
                        logger.warning("Answer still not grounded in the knowledge; withholding it")
                        return GroundedAnswer(NO_ANSWER)
        except Exception as e:
            logger.error("Error generating answer: %s", e)
            return GroundedAnswer(NO_ANSWER, "Unable to generate a response.")
        if is_insufficient(raw):
            return GroundedAnswer(NO_ANSWER)
        text, cited = citations(raw)
        return GroundedAnswer(text, cited=cited)

    def generate_answer_checked(
        self, question: str, context: str, company_name: Optional[str] = None, message: Optional[str] = None,
    ) -> Tuple[str, str]:
        """(answer, error): answer is NO_ANSWER when the knowledge does not answer the question."""
        if context and context.strip() and not llm_client.is_configured():
            return self.generate_answer(question, context, company_name), ""
        result = self.answer(question, context, company_name, message)
        return result.text, result.error

    def generate_answer(self, question: str, context: str, company_name: Optional[str] = None) -> str:
        """
        Generates a direct, factual answer using ONLY the retrieved context.
        Returns 'INSUFFICIENT_KNOWLEDGE' if context is missing or irrelevant.
        """
        if not context or not context.strip():
            logger.info("No context available for question: %s", question)
            return NO_ANSWER

        if not llm_client.is_configured():
            # Without any AI provider, return the top context snippet
            logger.info("No AI provider configured. Returning top context snippet.")
            return context[:400]
        return self.answer(question, context, company_name).text

    def condense_question(self, question: str, history: List[Dict[str, str]]) -> str:
        """
        A follow-up question rewritten to stand on its own, using the previous
        questions and answers. Falls back to the previous question plus the
        follow-up when the AI service is unavailable.
        """
        turns = [h for h in history if h.get("question")]
        if not turns:
            return question
        fallback = f"{turns[-1]['question']} {question}"
        if not llm_client.is_configured():
            return fallback
        lines = []
        for h in turns[-3:]:
            lines.append(f"Q: {h['question']}")
            if h.get("answer"):
                lines.append(f"A: {h['answer'][:1500]}")
        try:
            rewritten = llm_client.complete(
                CONDENSE_QUESTION_PROMPT.format(history="\n".join(lines), question=question), temperature=0.0)
        except Exception as e:
            logger.warning("Could not rewrite follow-up question: %s", e)
            return fallback
        rewritten = " ".join((rewritten or "").split()).strip('"')
        return rewritten if 3 <= len(rewritten) <= 1000 else fallback


# Global singleton FAQ agent
faq_agent = FAQAgent()
