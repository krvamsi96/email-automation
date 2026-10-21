"""
LangGraph Workflow Assembly for Intelligent Email Copilot.
Builds the complete state graph: email analysis (security screening, category and
request in one AI call), knowledge retrieval, grounded answer, reply writing,
quality validation, and Gmail draft creation.
"""

from langgraph.graph import StateGraph, END
from app.graph.state import CopilotState
from app.graph.nodes import nodes


class CopilotWorkflow:
    """Constructs and compiles the Intelligent Email Copilot LangGraph pipeline."""

    def __init__(self):
        workflow = StateGraph(CopilotState)

        # 1. Define all graph nodes
        workflow.add_node("load_emails", nodes.load_emails)
        workflow.add_node("pop_email", nodes.pop_email)
        workflow.add_node("analyze_email", nodes.analyze_email)
        workflow.add_node("flag_suspicious", nodes.flag_suspicious)
        workflow.add_node("skip_email", nodes.skip_email)
        workflow.add_node("retrieve_knowledge", nodes.retrieve_knowledge)
        workflow.add_node("generate_rag_answer", nodes.generate_rag_answer)
        workflow.add_node("handle_unknown_knowledge", nodes.handle_unknown_knowledge)
        workflow.add_node("generate_email_draft", nodes.generate_email_draft)
        workflow.add_node("validate_response", nodes.validate_response)
        workflow.add_node("prepare_escalation_draft", nodes.prepare_escalation_draft)
        workflow.add_node("create_gmail_draft", nodes.create_gmail_draft)

        # 2. Set entry point
        workflow.set_entry_point("load_emails")

        # 3. Queue checking loop
        workflow.add_conditional_edges(
            "load_emails",
            nodes.check_queue,
            {
                "has_emails": "pop_email",
                "empty": END,
            },
        )

        # 4. Analysis: suspicious -> flag, unrelated -> skip, otherwise -> knowledge search
        workflow.add_edge("pop_email", "analyze_email")
        workflow.add_conditional_edges(
            "analyze_email",
            nodes.route_after_analysis,
            {
                "suspicious": "flag_suspicious",
                "unrelated": "skip_email",
                "process": "retrieve_knowledge",
            },
        )
        for terminal in ("flag_suspicious", "skip_email"):
            workflow.add_conditional_edges(
                terminal,
                nodes.check_queue,
                {
                    "has_emails": "pop_email",
                    "empty": END,
                },
            )

        # 5. RAG retrieval
        workflow.add_conditional_edges(
            "retrieve_knowledge",
            nodes.route_knowledge_status,
            {
                "has_context": "generate_rag_answer",
                "no_context": "handle_unknown_knowledge",
            },
        )

        # 6. Response drafting
        workflow.add_edge("generate_rag_answer", "generate_email_draft")
        workflow.add_edge("handle_unknown_knowledge", "generate_email_draft")

        # 7. Response validation & bounded retry loop
        workflow.add_edge("generate_email_draft", "validate_response")
        workflow.add_conditional_edges(
            "validate_response",
            nodes.route_after_validation,
            {
                "valid": "create_gmail_draft",
                "retry": "generate_email_draft",
                "max_retries_exceeded": "prepare_escalation_draft",
            },
        )
        workflow.add_edge("prepare_escalation_draft", "create_gmail_draft")

        # 8. Draft created -> loop back to check next email in queue
        workflow.add_conditional_edges(
            "create_gmail_draft",
            nodes.check_queue,
            {
                "has_emails": "pop_email",
                "empty": END,
            },
        )

        self.app = workflow.compile()


# Global workflow instance
copilot_workflow = CopilotWorkflow()
app = copilot_workflow.app
