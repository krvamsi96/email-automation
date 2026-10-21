"""
Test isolation: point every data path at a temporary directory *before* the
application modules are imported, so tests never touch the real knowledge
base, database or Gmail tokens.

The sample knowledge base is built from tests/fixtures (never from documents a
user uploaded to the app), plus a generated two-page PDF with a running footer.

Performance: the SentenceTransformer embedding model is loaded once here,
before any test runs, so no individual test pays the cold-start penalty.
"""

import os
import shutil
import tempfile
from pathlib import Path

from tests.pdf_fixture import make_pdf

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
TEST_DATA = Path(tempfile.mkdtemp(prefix="copilot-tests-"))
TEST_DOCS = TEST_DATA / "documents"
TEST_DOCS.mkdir(parents=True)
for fixture in FIXTURES.iterdir():
    shutil.copy2(fixture, TEST_DOCS / fixture.name)

FOOTER = "Customer Help Centre FAQ - printed 01/02/2026 10:15"
make_pdf(TEST_DOCS / "FAQ.pdf", title="Customer Help Centre FAQ", pages=[
    ["How do I reset my password?",
     "Use the Forgot Password link on the login page. A reset link is emailed to you.",
     "What are the support hours?",
     "Support is available Monday to Friday, 9am to 6pm.",
     FOOTER, "Page 1 of 2"],
    ["How do I change my billing address?",
     "Open Account Settings, choose Billing and save the new address.",
     "Which payment methods are accepted?",
     "We accept credit cards and bank transfers.",
     FOOTER, "Page 2 of 2"],
])

os.environ.update({
    "DATA_DIR": str(TEST_DATA),
    "APP_DB_PATH": str(TEST_DATA / "copilot.db"),
    "CHROMA_PERSIST_DIRECTORY": str(TEST_DATA / "chroma"),
    "KNOWLEDGE_BASE_PATH": str(TEST_DOCS),
    "GMAIL_TOKEN_PATH": str(TEST_DATA / "token.json"),
    "GMAIL_CREDENTIALS_PATH": str(TEST_DATA / "credentials.json"),
    "APP_SECRET_KEY": "",
    "ADMIN_TOKEN": "",
    "PUBLIC_BASE_URL": "",
    # No real AI calls: every provider starts unconfigured (tests patch llm_client or set keys)
    "LLM_PROVIDER": "ollama",
    "LLM_FALLBACK_PROVIDERS": "",
    "OLLAMA_BASE_URL": "",
    "OLLAMA_MODEL": "",
    "GROQ_API_KEY": "",
    "GROQ_MODEL": "",
    "GEMINI_API_KEY": "",
    "GOOGLE_API_KEY": "",
    "GEMINI_MODEL": "",
})


def pytest_configure(config):
    """Warm up the embedding model once per test session before any test starts.

    The SentenceTransformer model (~85 MB) takes several seconds to load on
    first use. Without this hook every test suite that is first to call an
    embed pays that cost mid-test, making it appear slow. Loading it here
    amortises the cost over the whole session.
    """
    try:
        from app.rag.embeddings import get_embeddings
        get_embeddings().warm_up()
    except Exception:  # pragma: no cover
        pass  # test environment may not have the model; let individual tests fail naturally


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(TEST_DATA, ignore_errors=True)
