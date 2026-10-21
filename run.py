#!/usr/bin/env python3
"""
Intelligent Email Copilot - Unified CLI Runner.
Provides commands for running the email agent, ingesting knowledge, and running diagnostics.
"""

import argparse
from colorama import Fore, Style, init

from app.config import Config
from app.rag.ingestion import ingestion_pipeline
from app.rag.retriever import knowledge_retriever
from app.llm.client import llm_client
from app.llm.providers import PROVIDERS
from app.gmail.auth import gmail_auth
from app.gmail.reader import gmail_reader
from app.main import run_copilot

init(autoreset=True)


def cmd_status():
    """Displays current system configuration status."""
    print(f"\n{Fore.CYAN}{Style.BRIGHT}=== Intelligent Email Copilot System Status ==={Style.RESET_ALL}")
    safe_info = Config.to_safe_dict()
    for k, v in safe_info.items():
        color = Fore.GREEN if v is True else (Fore.RED if v is False else Fore.WHITE)
        print(f"  {k:<30}: {color}{v}{Style.RESET_ALL}")
    print("=" * 48 + "\n")


def cmd_ingest(args):
    """Ingests documents from data/documents into ChromaDB."""
    print(Fore.CYAN + f"\n[INFO] Starting document ingestion from: {Config.KNOWLEDGE_BASE_PATH}..." + Style.RESET_ALL)
    added = ingestion_pipeline.run(reset_existing=args.reset)
    print(Fore.GREEN + f"[SUCCESS] Ingested {added} chunks into ChromaDB at {Config.CHROMA_PERSIST_DIRECTORY}!\n" + Style.RESET_ALL)


def cmd_test_rag(args):
    """Tests semantic search retrieval for a question."""
    query = args.query
    print(Fore.CYAN + f"\n[INFO] Testing RAG retrieval for query: '{query}'" + Style.RESET_ALL)
    results = knowledge_retriever.retrieve(query)
    if not results:
        print(Fore.YELLOW + "[WARNING] No matching documents found in vector store.\n" + Style.RESET_ALL)
        return

    print(Fore.GREEN + f"[INFO] Retrieved {len(results)} matching document chunks:\n" + Style.RESET_ALL)
    for i, doc in enumerate(results, 1):
        print(f"{Fore.MAGENTA}--- Result {i} (Source: {doc.metadata.get('source', 'Unknown')}) ---{Style.RESET_ALL}")
        print(doc.page_content.strip())
        print()


def cmd_test_llm(args):
    """Tests the AI providers (all configured ones, or the one given with --provider)."""
    names = [args.provider] if args.provider else list(PROVIDERS)
    for name in names:
        label = PROVIDERS[name].label
        print(Fore.CYAN + f"\n[INFO] Testing {label}..." + Style.RESET_ALL)
        res = llm_client.test_connection(name, args.model)
        if res.get("status") == "SUCCESS":
            print(Fore.GREEN + f"[SUCCESS] {label} ({res['model']}) replied in {res['seconds']}s: {res['message']}" + Style.RESET_ALL)
        elif res.get("status") == "NOT_CONFIGURED":
            print(Fore.YELLOW + f"[INFO] {res.get('message')}" + Style.RESET_ALL)
        else:
            print(Fore.RED + f"[FAILED] {label} ({res.get('model')}): {res.get('error')}" + Style.RESET_ALL)
    active = llm_client.active()
    print(Fore.WHITE + f"\nActive provider: {active['label']} (model: {active['model']}); "
          f"order tried: {', '.join(llm_client.settings().chain)}\n" + Style.RESET_ALL)


def cmd_test_gmail():
    """Tests Gmail OAuth credentials and reading."""
    print(Fore.CYAN + "\n[INFO] Testing Gmail service authentication..." + Style.RESET_ALL)
    service = gmail_auth.get_service()
    if not service:
        print(Fore.YELLOW + "[INFO] Gmail service is not yet authenticated. Ensure credentials.json is provided.\n" + Style.RESET_ALL)
        return

    print(Fore.GREEN + "[SUCCESS] Gmail service authenticated successfully!" + Style.RESET_ALL)
    print(Fore.CYAN + "[INFO] Testing fetching unanswered emails..." + Style.RESET_ALL)
    emails = gmail_reader.fetch_unanswered_emails(max_results=5)
    print(Fore.GREEN + f"[SUCCESS] Found {len(emails)} unanswered email(s).\n" + Style.RESET_ALL)


def main():
    parser = argparse.ArgumentParser(
        description="Intelligent Email Copilot - AI Email Automation System",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available sub-commands")

    # Command: run email
    subparsers.add_parser("email", help="Run the full email processing workflow")

    # Command: status
    subparsers.add_parser("status", help="Inspect configuration status")

    # Command: ingest
    ingest_p = subparsers.add_parser("ingest", help="Ingest documents into ChromaDB")
    ingest_p.add_argument("--reset", action="store_true", help="Reset existing collection before ingesting")

    # Command: test-rag
    rag_p = subparsers.add_parser("test-rag", help="Test RAG retrieval for a question")
    rag_p.add_argument("query", type=str, help="Search query or test question")

    # Command: test-llm
    llm_p = subparsers.add_parser("test-llm", help="Test the AI providers (Ollama, Groq, Gemini)")
    llm_p.add_argument("--provider", choices=list(PROVIDERS), help="Test only this provider")
    llm_p.add_argument("--model", help="Model to test instead of the configured one")

    # Command: test-gmail
    subparsers.add_parser("test-gmail", help="Test Gmail API authentication and reading")

    # Command: web
    web_p = subparsers.add_parser("web", help="Start the interactive dynamic Web Dashboard")
    web_p.add_argument("--port", type=int, default=8000, help="Port to bind web server (default: 8000)")
    web_p.add_argument("--host", type=str, default="127.0.0.1", help="Host to bind (default: 127.0.0.1)")

    args = parser.parse_args()

    if args.command == "email" or args.command is None:
        run_copilot()
    elif args.command == "web":
        from app.web.server import run_server
        run_server(host=args.host, port=args.port)
    elif args.command == "status":
        cmd_status()
    elif args.command == "ingest":
        cmd_ingest(args)
    elif args.command == "test-rag":
        cmd_test_rag(args)
    elif args.command == "test-llm":
        cmd_test_llm(args)
    elif args.command == "test-gmail":
        cmd_test_gmail()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
