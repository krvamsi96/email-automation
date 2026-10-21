"""
Unit and integration tests for the RAG pipeline: loading, cleaning, chunking,
local embeddings, ChromaDB storage and hybrid retrieval.
"""

from pathlib import Path

from app.agents.faq_agent import faq_agent, is_insufficient
from app.config import Config
from app.rag.embeddings import get_embeddings
from app.rag.ingestion import IngestionPipeline, UniversalDocumentLoader, clean_text, ingestion_pipeline, strip_running_lines
from app.rag.lexical import BM25Index, tokenize
from app.rag.retriever import KnowledgeRetriever, knowledge_retriever
from app.rag.vector_store import VectorStoreManager, vector_store_manager
from app.services.retrieval import find_knowledge
from tests.pdf_fixture import make_pdf

DOCS = Path(Config.KNOWLEDGE_BASE_PATH)


def test_local_embeddings():
    embeddings = get_embeddings()
    vec = embeddings.embed_query("password reset")
    assert isinstance(vec, list)
    assert len(vec) == 384  # all-MiniLM-L6-v2 dimension is 384
    assert abs(sum(v * v for v in vec) - 1.0) < 1e-3  # normalised: dot product == cosine


def test_pdf_loading_removes_running_footer_and_keeps_pages():
    docs = UniversalDocumentLoader.load_pdf(DOCS / "FAQ.pdf")
    assert [d.metadata["page"] for d in docs] == [1, 2]
    text = "\n".join(d.page_content for d in docs)
    assert "Forgot Password" in text and "billing address" in text.lower()
    assert "printed" not in text and "Page 1 of 2" not in text  # running footer removed
    assert docs[0].metadata["title"] == "Customer Help Centre FAQ"


def test_running_lines_only_strip_page_edges():
    quoted = "Please use the Forgot Password option to reset your password by 30 June."
    # e-mail thread printed from a browser: the quoted message ends both pages, just above the footer
    two = strip_running_lines([f"Reply from the helpdesk\n{quoted}\nPrinted 9/28/26 10:09\nabout:blank 1/2",
                               f"Original message\n{quoted}\nPrinted 9/28/26 10:09\nabout:blank 2/2"])
    assert two == [f"Reply from the helpdesk\n{quoted}", f"Original message\n{quoted}"]

    three = strip_running_lines([f"Staff Handbook Section\n{name} text\nPage {n}"
                                 for n, name in enumerate(("alpha", "beta", "gamma"), start=1)])
    assert three == ["alpha text", "beta text", "gamma text"]

    body = "First paragraph.\n\nSecond paragraph."
    assert strip_running_lines([f"{body}\nPage 1", f"{body}\nPage 2"]) == [body, body]  # paragraphs kept


def test_clean_text_repairs_pdf_artifacts():
    assert clean_text("conﬁgure the recommen-\ndation\n\n\n\nnext") == "configure the recommendation\n\nnext"


def test_csv_loading():
    docs = UniversalDocumentLoader.load_csv(DOCS / "hr_support_faq.csv")
    assert len(docs) >= 3
    assert all(d.metadata["title"] for d in docs)


def test_chunks_cross_pages_and_skip_repeated_passages(tmp_path):
    repeated = ("The duplicate milestone entry was deleted from the portal and the new due date "
                "of 30 September was kept for the recommendation.")
    pdf = make_pdf(tmp_path / "thread.pdf", title="Thread", pages=[
        ["Reply: Needful has been done. Please check.", repeated, "Footer printed 9/28/26 10:09", "about:blank 1/2"],
        [repeated, "Footer printed 9/28/26 10:09", "about:blank 2/2"],
    ])
    chunks = IngestionPipeline(chunk_size=200, chunk_overlap=0, manager=VectorStoreManager(
        persist_directory=str(tmp_path / "db"), collection_name="t")).chunk_file(pdf, "doc1")
    texts = [c.page_content for c in chunks]
    assert sum(repeated[:40] in t for t in texts) == 1, texts  # quoted copy indexed once
    assert all("about:blank" not in t for t in texts)
    assert {c.metadata["page"] for c in chunks} <= {1, 2}
    assert [c.metadata["chunk"] for c in chunks] == list(range(len(chunks)))


def test_keyword_index_matches_identifiers():
    index = BM25Index(["a", "b"], ["Username dept_user01 forgot the password", "General portal guidance"])
    top = index.search("password for dept_user01", 2)
    assert top[0][0] == "a" and top[0][2] > 0.9
    assert tokenize("PA-23 Abc#2021 passwords") == ["pa", "23", "abc", "2021", "password"]


def test_ingestion_and_chroma_retrieval():
    added = ingestion_pipeline.run(reset_existing=True)
    assert added > 0
    assert vector_store_manager.count() == added
    assert vector_store_manager.index_is_current()

    results = knowledge_retriever.retrieve("How do I reset my password?")
    assert len(results) > 0
    combined_text = " ".join(d.page_content for d in results).lower()
    assert "password" in combined_text
    assert "forgot password" in combined_text or "login" in combined_text


def test_retrieval_threshold_and_multi_query():
    ingestion_pipeline.run(reset_existing=True)
    found = find_knowledge("default", ["When is salary paid?", "How do I connect to the VPN?"])
    docs = set(found.sources)
    assert {"hr_support_faq.csv", "it_support_faq.md"} <= docs  # one query per topic, both covered
    assert found.context.startswith("[1] Source: ")

    unrelated = find_knowledge("default", ["What is the quantum telemetry frequency of Martian satellite Alpha?"])
    assert not unrelated.found and unrelated.context == ""

    # A per-call threshold never changes the shared retriever
    before = knowledge_retriever.min_relevance
    find_knowledge("default", ["password"], min_relevance=0.9)
    assert knowledge_retriever.min_relevance == before


def test_outdated_index_is_detected(tmp_path):
    manager = VectorStoreManager(persist_directory=str(tmp_path / "db"), collection_name="old")
    manager.client.get_or_create_collection("old", metadata={"hnsw:space": "cosine"})  # made by an older version
    assert not manager.index_is_current()
    manager.reset()
    assert manager.index_is_current()
    assert KnowledgeRetriever(manager=manager).search("anything") == []


def test_unknown_question_handling():
    answer = faq_agent.generate_answer("What is the quantum telemetry frequency of Martian satellite Alpha?", context="")
    assert answer == "INSUFFICIENT_KNOWLEDGE"


def test_insufficient_answer_detection():
    assert is_insufficient("INSUFFICIENT_KNOWLEDGE")
    assert is_insufficient("I'm sorry, I don't have that information.")
    assert is_insufficient("")
    # A real answer that mentions a gap later on is still an answer
    assert not is_insufficient("Use the Forgot Password link. The link validity is not stated.")
    assert not is_insufficient("If you don't know your username, contact your nodal officer.")
