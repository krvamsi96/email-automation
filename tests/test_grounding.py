"""
Knowledge accuracy: correspondence-aware indexing, chunk sizing, thread context,
context construction, grounded answering (citations, exact-value check) and
follow-up questions. Uses a synthetic e-mail thread, not the user's documents.
"""

from unittest.mock import patch

from app.agents.faq_agent import NO_ANSWER, FAQAgent, citations, unsupported_values
from app.rag.correspondence import split_messages
from app.rag.embeddings import get_embeddings
from app.rag.ingestion import IngestionPipeline, embedding_text
from app.rag.lexical import tokenize
from app.rag.retriever import LEXICAL_MIN_TERMS, KnowledgeRetriever
from app.rag.vector_store import VectorStoreManager
from app.services.retrieval import cited_evidence, find_knowledge
from tests.test_api import Api, new_workspace, run, start, no_real_token_revocation  # noqa: F401
from tests.test_knowledge_test import FAQ, ask

# A printed thread, newest first: the reply, the request, then a re-printed copy of the request.
THREAD = """desk < desk@acme.example >
Re: Parking permit for Building C
Facilities Desk < desk@acme.example >
Mon, 03 Feb 2025 10:15:00 AM +0000
To "Jane Roe"<jane.roe@acme.example>
Cc "Ops Team"<ops@acme.example>,"Security
Office"<security@acme.example>
Dear Jane,
Your parking permit P-4471 for Building C has been issued. It is valid until 31 March 2025.
Regards,
Facilities Desk
From: Jane Roe <jane.roe@acme.example>
To: "Facilities Desk"<desk@acme.example>
Cc: "Ops Team"<ops@acme.example>
Date: Fri, 31 Jan 2025 09:00:00 +0000
Subject: Parking permit for Building C
Hello,
Please issue a parking permit for Building C for my new car, registration KA01AB1234.
Thanks, Jane
Jane Roe < jane.roe@acme.example >
Fri, 31 Jan 2025 9:00:05 AM +0000
To "Facilities Desk"<desk@acme.example>
Hello,
Please issue a parking permit for Building C for my new car, registration KA01AB1234.
Thanks, Jane
"""


def thread_kb(tmp_path):
    path = tmp_path / "Re_ Parking permit for Building C.txt"
    path.write_text(THREAD, encoding="utf-8")
    manager = VectorStoreManager(persist_directory=str(tmp_path / "db"), collection_name="thread")
    pipeline = IngestionPipeline(manager=manager)
    chunks = pipeline.chunk_file(path, "doc1")
    pipeline.store_chunks("doc1", chunks)
    return chunks, KnowledgeRetriever(manager=manager)


def test_thread_is_split_into_messages_with_sender_and_date():
    messages = split_messages(THREAD, "Re Parking permit for Building C")
    assert [m.sender for m in messages] == ["Facilities Desk", "Jane Roe", "Jane Roe"]
    assert [m.timestamp for m in messages] == ["2025-02-03T10:15:00", "2025-01-31T09:00:00", "2025-01-31T09:00:05"]
    assert messages[1].subject == "Parking permit for Building C"
    assert messages[0].body.startswith("Dear Jane,") and "P-4471" in messages[0].body
    assert all("@acme.example" not in m.body for m in messages)  # recipient blocks are not content
    assert split_messages("# FAQ\n\nHow do I reset my password?\nUse the Forgot Password link.") == []


def test_messages_are_indexed_once_with_metadata(tmp_path):
    chunks, _ = thread_kb(tmp_path)
    assert len(chunks) == 2  # the re-printed copy of the request is dropped
    reply, request = chunks
    assert reply.metadata["sender"] == "Facilities Desk" and "has been issued" in reply.page_content
    assert request.metadata["subject"] == "Parking permit for Building C"
    assert "Please issue a parking permit" in request.page_content
    assert "From: Facilities Desk" in embedding_text(reply)


def test_every_chunk_fits_the_embedding_window(tmp_path):
    path = tmp_path / "manual.txt"
    path.write_text(" ".join(f"Step {i}: open panel {i} and record reading R-{i:04d}." for i in range(400)), encoding="utf-8")
    chunks = IngestionPipeline(chunk_size=6000, chunk_overlap=0, manager=VectorStoreManager(
        persist_directory=str(tmp_path / "db"), collection_name="m")).chunk_file(path, "doc2")
    emb = get_embeddings()
    assert len(chunks) > 1
    assert all(emb.count_tokens(embedding_text(c)) <= emb.max_tokens for c in chunks)
    assert "R-0399" in chunks[-1].page_content  # nothing at the end of the document is lost


def test_request_is_returned_with_its_reply_in_date_order(tmp_path):
    _, retriever = thread_kb(tmp_path)
    found = find_knowledge("x", ["Please issue a parking permit for Building C, registration KA01AB1234"],
                           retriever=retriever)
    assert "Please issue a parking permit" in found.context and "has been issued" in found.context
    assert found.context.index("Please issue a parking permit") < found.context.index("has been issued")
    assert "Message — From: Jane Roe | Date: Fri, 31 Jan 2025 09:00:00 +0000" in found.context
    assert "Message — From: Facilities Desk" in found.context


def test_one_generic_word_is_not_a_keyword_match(tmp_path):
    _, retriever = thread_kb(tmp_path)
    assert len(tokenize("please check")) < LEXICAL_MIN_TERMS
    assert find_knowledge("x", ["regards"], min_relevance=0.95, retriever=retriever).evidence == []


def test_unsupported_values_and_citations():
    context = "Permit P-4471 is valid until 31 March 2025. Contact desk@acme.example or https://acme.example/permits"
    assert unsupported_values("Permit P-4471 [1] is valid until 31 March 2025 [1].", context) == []
    assert unsupported_values("Write to desk@acme.example or see https://acme.example/permits.", context) == []
    assert unsupported_values("Your permit is P-9999.", context) == ["P-9999"]
    assert unsupported_values("Call 1800-555-0100 or mail help@acme.example.", context) == ["help@acme.example", "1800-555-0100"]
    assert citations("Issued [2]. Valid until March [1][2].") == ("Issued. Valid until March.", [2, 1])
    # gpt-oss models cite with lenticular / full-width brackets
    assert citations("Use Forgot Password【7】, then call ［2］.") == ("Use Forgot Password, then call.", [7, 2])
    assert citations("Send your Email ID (see 【7】). Details (as listed) [1].") == ("Send your Email ID. Details (as listed).", [7, 1])
    evidence = [{"document": "a"}, {"document": "b"}]
    assert cited_evidence(evidence, [2]) == [{"document": "b"}]
    assert cited_evidence(evidence, []) == evidence


def test_answer_with_invented_value_is_regenerated_or_withheld():
    context = "[1] Source: permits.txt\nPermit P-4471 for Building C has been issued."
    agent = FAQAgent()
    with patch("app.llm.client.llm_client.is_configured", return_value=True), \
         patch("app.llm.client.llm_client.complete",
               side_effect=["Your permit number is P-9999 [1].", "Permit P-4471 has been issued [1]."]) as llm:
        result = agent.answer("What is my permit number?", context)
    assert result.text == "Permit P-4471 has been issued." and result.cited == [1]
    assert llm.call_count == 2 and "P-9999" in llm.call_args_list[1].args[0]
    assert "ONLY" in llm.call_args_list[0].kwargs["system_prompt"]  # rules are sent as a system message

    with patch("app.llm.client.llm_client.is_configured", return_value=True), \
         patch("app.llm.client.llm_client.complete", return_value="Permit P-9999 [1]."):
        assert agent.answer("What is my permit number?", context).text == NO_ANSWER


def test_follow_up_question_is_rewritten_and_answered():
    def fake_llm(prompt, **kwargs):
        if "STANDALONE QUESTION" in prompt:
            return "How long is the password reset link valid?"
        if "valid" in prompt and "How long" in prompt:
            return "The reset link is valid for 30 minutes [1]."
        return "Click Forgot Password on the login page [1]."

    async def go():
        c = await start()
        api = await new_workspace(Api(c), "Follow-ups")
        try:
            await api.upload([("it_faq.md", FAQ)])
            await api.wait("/api/documents", lambda d: d["items"][0]["status"] == "ready")
            with patch("app.llm.client.llm_client.is_configured", return_value=True), \
                 patch("app.llm.client.llm_client.complete", side_effect=fake_llm):
                first = await ask(api, "How do I reset my password?")
                assert first["status"] == "answered" and first["answer"] == "Click Forgot Password on the login page."
                r = await api.send("POST", "/api/knowledge/ask", {"question": "How long is it valid?",
                                                                  "follow_up_to": first["id"]}, status=202)
                done = await api.wait(f"/api/knowledge/tests/{r['id']}", lambda t: t["status"] != "processing")
            assert done["follow_up_to"] == first["id"]
            assert done["search_question"] == "How long is the password reset link valid?"
            assert done["answer"] == "The reset link is valid for 30 minutes."
            assert [s["document"] for s in done["sources"]] == ["it_faq.md"]
            await api.send("POST", "/api/knowledge/ask", {"question": "And then?", "follow_up_to": "nope"}, status=404)
        finally:
            await api.send("DELETE", f"/api/workspaces/{api.ws}")
            await c.close()
    run(go())
