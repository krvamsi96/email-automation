"""
Intelligent Email Copilot - Main Application Entrypoint.
Orchestrates autonomous email ingestion, RAG search, AI generation (Ollama / Groq / Gemini),
validation, and Gmail draft creation with human-in-the-loop approval.
"""

from colorama import Fore, Style, init

from app.config import Config
from app.llm.client import llm_client
from app.graph.workflow import app
from app.graph.state import build_initial_state

init(autoreset=True)


def print_banner():
    active = llm_client.active()
    banner = f"""
{Fore.CYAN}{Style.BRIGHT}====================================================================
           INTELLIGENT EMAIL COPILOT
   Autonomous RAG Email Assistant with Ollama / Groq / Gemini & Gmail
===================================================================={Style.RESET_ALL}
{Fore.WHITE}Project: {Config.PROJECT_NAME}
Company: {Config.COMPANY_NAME}
AI Provider: {active['label']} (Model: {active['model']}; order: {', '.join(llm_client.settings().chain)})
Embeddings: {Config.EMBEDDING_MODEL} (Local Sentence-Transformers)
Vector Store: ChromaDB ({Config.CHROMA_PERSIST_DIRECTORY})
Safety Policy: AUTOMATIC SENDING DISABLED (Gmail Drafts Only)
====================================================================
"""
    print(banner)


def run_copilot(custom_emails=None):
    """
    Runs the complete email processing workflow.
    Optionally accepts a list of pre-defined email dictionaries for testing.
    """
    print_banner()

    initial_state = build_initial_state()
    if custom_emails:
        initial_state["emails_queue"] = custom_emails
        print(Fore.GREEN + f"[INFO] Running copilot with {len(custom_emails)} custom queued email(s)." + Style.RESET_ALL)
    else:
        print(Fore.CYAN + "[INFO] Connecting to Gmail to fetch unhandled inbox emails..." + Style.RESET_ALL)

    run_config = {"recursion_limit": 100}

    try:
        final_state = initial_state
        for output in app.stream(initial_state, run_config):
            for node_name, state_update in output.items():
                print(Fore.MAGENTA + f"[STEP COMPLETED] Node: {node_name}" + Style.RESET_ALL)
                final_state = {**final_state, **state_update}

        print("\n" + "=" * 68)
        print(Fore.GREEN + Style.BRIGHT + "   EXECUTION SUMMARY" + Style.RESET_ALL)
        print("=" * 68)
        print(f"Total Emails Processed : {final_state.get('processed_count', 0)}")
        print(f"Gmail Drafts Created   : {Fore.GREEN}{final_state.get('drafts_created_count', 0)}{Style.RESET_ALL}")
        print(f"Security Threats Flagged: {Fore.RED if final_state.get('flagged_count', 0) > 0 else Fore.WHITE}{final_state.get('flagged_count', 0)}{Style.RESET_ALL}")
        print(f"Skipped / Unrelated    : {final_state.get('skipped_count', 0)}")
        print("=" * 68)
        print(Fore.YELLOW + Style.BRIGHT + "[REMINDER] Emails were NOT sent automatically." + Style.RESET_ALL)
        print(Fore.YELLOW + "Please open Gmail to review, edit, and manually send generated drafts." + Style.RESET_ALL)
        print("=" * 68 + "\n")

        return final_state

    except Exception as e:
        print(Fore.RED + f"\n[ERROR] An error occurred during workflow execution: {e}" + Style.RESET_ALL)
        return None


if __name__ == "__main__":
    run_copilot()
