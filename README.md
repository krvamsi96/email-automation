# Intelligent Email Copilot

An AI email assistant that reads incoming Gmail, finds answers in your company's
own documents, and prepares grounded replies as **Gmail drafts for human review**.
A person reviews, edits, approves and **sends every email manually** — the
application has no code path that sends mail.

```
Gmail email → AI understands it → Company Knowledge Base → grounded reply
            → Gmail draft → Human review / edit / approve → Human sends in Gmail
```

This README is developer documentation. The client-facing web app deliberately
uses business terms (Knowledge Base, AI Response, Approvals) instead of the
technology names below.

---

## Two independent modes

| Mode | Needs Gmail? | Flow |
|---|---|---|
| **Knowledge Base Test** (`#/knowledge-test`) | No | Upload document → processing → knowledge ready → ask a question → knowledge search → AI answer + sources, or "no reliable information → human review" |
| **Email Assistant** (`#/email-assistant`) | Yes | Gmail → email understood → question detected → knowledge search → AI reply → Gmail draft → human review / edit / approve → human sends |

Both modes call the same services, so a question tested in mode 1 gets the same
knowledge in mode 2:

```
services/retrieval.py   find_knowledge()    ← used by both workflows
services/answers.py     generate_answer()   ← AI answer from retrieved knowledge (Ollama / Groq / Gemini)
graph/knowledge_test.py receive_question → retrieve_knowledge → generate_answer → validate_answer → return_result
graph/workflow.py       email: analyze (security + category + request, one AI call) → retrieve_knowledge → answer → write → validate → Gmail draft
services/processing.py  email service (inbox checks)      services/drafts.py  draft/approval service
```

Gmail is optional: nothing touches Gmail at startup (a legacy `token.json` is
adopted in a background thread), and the knowledge endpoints never need it.
Knowledge Test API: `POST /api/knowledge/ask {"question"}` (202, runs in the
background) → `GET /api/knowledge/tests/{id}` returns
`{question, answer, sources: [{document, excerpts}], status: answered | no_knowledge | ai_unavailable}`
(`no_knowledge` answers "The requested information is not available in the Knowledge Base.");
`GET/DELETE /api/knowledge/tests` for history. `GET /api/emails/{id}` includes
`result: {email_id, question, answer, sources, draft_created, status}`.

---

## 1. Architecture

```
Browser (web app, no secrets)
   │  same-origin JSON API  (CSRF header required for changes)
   ▼
aiohttp backend  ─ app/web/server.py
   │
   ├── services/processing.py   inbox checks, per-email AI runs (background jobs)
   ├── services/drafts.py       review → edit → approve / reject, Gmail sent-status sync
   ├── services/knowledge.py    upload, index, reprocess, delete, search
   ├── services/gmail_accounts.py  Google OAuth (web flow + PKCE), encrypted tokens
   ├── services/workspaces.py   per-client branding + settings
   │
   ├── LangGraph workflow  app/graph/  (analyze → retrieve → answer → write → validate → Gmail draft)
   ├── Knowledge: LangChain + ChromaDB (one collection per workspace, chunks tagged by document)
   │     hybrid search: semantic (embeddings) + keyword (BM25), merged by rank fusion,
   │     re-ranked by a local cross-encoder; e-mail threads indexed per message
   ├── Embeddings: sentence-transformers all-MiniLM-L6-v2 (local, loaded at startup in the background)
   ├── LLM: Ollama, Groq or Google Gemini (app/llm/: providers.py, client.py, settings.py)
   └── SQLite (data/copilot.db): workspaces, accounts, documents, emails, drafts, activity
```

Key properties

* **No automatic sending.** Gmail access is used to list/read messages and to
  create, update and delete drafts only. Tests assert `send` is never called.
* **Real deletes.** Every knowledge chunk carries its document id, so deleting a
  document removes the file, its record and exactly its indexed chunks.
* **Relevance threshold.** Passages below `RETRIEVAL_MIN_RELEVANCE` (semantic match,
  default 0.30) are ignored unless they contain nearly all of the question's
  distinctive keywords (at least two), so off-topic emails get a safe acknowledgement
  (or are left for a person) instead of an answer "grounded" in unrelated text.
* **Re-ranking.** A local cross-encoder (`RERANKER_MODEL`) re-scores the surviving
  passages against the question, puts the passage that answers it first and drops
  passages that do not. Without it (offline, or `none`) the hybrid ranking is used.
* **Correspondence-aware index.** E-mail threads (printed PDFs, TXT, DOCX) are split
  into their individual messages from the headers themselves (From/Date/Subject
  blocks, web-mail print views, "On … wrote:"). Each chunk belongs to one message and
  carries its sender, date and subject; recipient address blocks are left out and
  repeated (quoted or re-printed) copies are indexed once. A matched message is given
  to the AI with the messages just before and after it in the thread, in date order,
  so a request is always seen with the reply that answered it.
* **Clean, self-maintaining index.** Ingestion removes running page headers/footers,
  repairs PDF artefacts, keeps passages that cross page breaks together and embeds
  each passage with its title/subject/sender. Every chunk fits the embedding model's
  input window (text beyond it would be silently ignored by the model). The index
  records its embedding model, chunk settings and pipeline version; if they change
  the knowledge is rebuilt automatically at startup.
* **Grounded answers.** The rules are sent as a system message; the AI answers only
  from the numbered excerpts and cites them ([1], [2]), and only the cited excerpts
  are shown as sources. Exact values in an answer (numbers, IDs, codes, e-mail
  addresses, links) must occur in the knowledge: an answer that invents one is
  regenerated once and otherwise withheld. When the Knowledge Base does not contain
  the answer, the Knowledge Base Test returns *"The requested information is not
  available in the Knowledge Base."* and the Email Assistant drafts a holding reply.
* **Follow-up questions.** In the Knowledge Base Test, *Ask a follow-up* rewrites the
  new question into a standalone one from the earlier questions and answers
  (`POST /api/knowledge/ask {"question", "follow_up_to"}`) before searching.
* **Failures are surfaced, not hidden.** If every configured AI provider fails, the
  email is marked *Needs Human Review* and the draft is flagged.
* **Choice of AI provider.** Ollama (local), Groq and Google Gemini sit behind one
  client (`app/llm/client.py`). Requests go to the primary provider and, when it fails
  (bad key, unknown model, rate limit, outage, timeout), to the fallback providers in
  order. Each attempt has an overall deadline; temporary failures are retried. Replies
  that were cut off at the token limit or blocked by the provider are treated as
  failures, never saved as drafts. The provider order and each provider's model are
  chosen in *Settings → Advanced → AI service* from the models each provider lists
  live; that choice is stored in the database and overrides the `.env` defaults.
* **Multi-client.** Workspaces isolate accounts, knowledge, emails, drafts,
  settings and branding (product name, company name, colour, logo, support email).

---

## 2. Setup

Requirements: Python 3.10+.

```bash
python -m venv venv
venv\Scripts\activate            # Windows   (macOS/Linux: source venv/bin/activate)
pip install -r requirements.txt
copy .env.example .env           # then edit .env
```

Minimum configuration in `.env`:

| Variable | Purpose |
|---|---|
| `LLM_PROVIDER` / `LLM_FALLBACK_PROVIDERS` | AI provider answering first (`ollama`, `groq` or `gemini`) and the ones tried when it fails |
| `GROQ_API_KEY` / `GEMINI_API_KEY` | Keys for the hosted providers (backend only; never sent to the browser or logged) |
| `OLLAMA_BASE_URL` / `OLLAMA_MODEL` | Local Ollama server and model (no key; `ollama pull qwen3:8b`) |
| `GROQ_MODEL` / `GEMINI_MODEL` | Default model per provider (blank = first model the provider lists) |
| `COMPANY_NAME` / `PRODUCT_NAME` | Defaults for the first workspace (editable in Settings) |
| `PUBLIC_BASE_URL` | Public URL of the app when not on `http://127.0.0.1:8000`; used for the OAuth redirect |
| `ADMIN_TOKEN` | Optional. When set, Advanced Settings require this key. When empty, only browsers on the server machine (loopback) are administrators |
| `APP_SECRET_KEY` | Optional Fernet key for encrypting stored Gmail tokens. If empty a key is generated in `data/.secret_key` |

See `.env.example` for all options.

### Gmail sign-in (one-time, administrator)

1. In Google Cloud Console enable the **Gmail API**.
2. Configure the OAuth consent screen and add test users (or publish the app).
3. Create an **OAuth client ID → Web application**. Add the redirect URI shown in
   *Settings → Advanced → Gmail sign-in*, e.g.
   `http://127.0.0.1:8000/api/email-accounts/oauth/callback`.
4. Download the client JSON and upload it in *Settings → Advanced* (or save it as
   `credentials.json`).

Users then click **Email Accounts → Add Email Account → Continue with Google**.
Scopes requested: `gmail.modify` (read, label) and `gmail.compose` (drafts).
Disconnecting revokes the token with Google and deletes it from the database.

---

## 3. Running

```bash
python run.py web                  # web app on http://127.0.0.1:8000
python run.py web --port 8080 --host 0.0.0.0
```

Command-line tools (developer use):

```bash
python run.py status               # configuration summary (no secrets)
python run.py ingest [--reset]     # index data/documents for the default workspace
python run.py test-rag "question"  # retrieval check
python run.py test-llm             # test Ollama, Groq and Gemini (--provider groq --model ... for one)
python run.py test-gmail           # legacy single-account token.json check
python run.py email                # legacy CLI run over token.json inbox
```

Documents dropped into `data/documents/` are registered in the default
workspace's Knowledge Base on startup.

---

## 4. Using the web app

| Page | What it does |
|---|---|
| Dashboard | Live counts (emails, pending reviews, drafts, documents, accounts), recent activity, setup checklist, *Check for new emails* |
| Emails | Search/filter emails; status, category and draft status. Detail view shows original email, conversation thread, knowledge used and the AI response; *Process again* |
| Knowledge Base | Drag & drop / multi-file upload (PDF, DOCX, TXT, Markdown, CSV), live processing status, search, reprocess, retry, delete with confirmation |
| Drafts | All AI drafts by status; *Refresh from Gmail* detects drafts a person sent or deleted in Gmail; retry failed saves |
| Approvals | Queue of drafts pending human review → review screen: edit, save, reject (removes Gmail draft), approve |
| Email Accounts | Connect Gmail (OAuth), test connection, reconnect, disconnect |
| Settings | General (branding, logo), Email (automatic checking, look-back, limits), Knowledge, AI Preferences (tone, signature, behaviour when no answer is found), Notifications (desktop), Security, Advanced (admin: AI provider, fallbacks and model per provider with live model lists and tests, Gmail sign-in setup, match threshold, workspaces) |

Status vocabulary (same everywhere): Ready, Processing, Failed, Processed, Needs
Human Review, Security Review, Skipped, Pending Review, Edited, Approved, Sent,
Rejected, Connected, Disconnected, Connection Error.

---

## 5. API

All endpoints are under `/api`, scoped to the workspace in the `X-Workspace-Id`
header. Non-GET requests require `X-Requested-With: copilot`.

```
GET    /api/app                                  branding, capabilities, workspaces
GET    /api/dashboard/stats
GET    /api/activity
GET    /api/emails?status=&q=        GET /api/emails/{id}
POST   /api/emails/check  (202)      GET /api/emails/check   (job progress)
POST   /api/emails/{id}/process
GET    /api/documents                POST /api/documents/upload (multipart "files")
DELETE /api/documents/{id}           POST /api/documents/{id}/reprocess
POST   /api/documents/reprocess-all  GET  /api/knowledge/search?q=
GET    /api/drafts?status=           GET  /api/drafts/{id}
PUT    /api/drafts/{id}              POST /api/drafts/{id}/approve | reject | retry
POST   /api/drafts/sync
GET    /api/email-accounts           POST /api/email-accounts/connect
GET    /api/email-accounts/oauth/callback
POST   /api/email-accounts/{id}/test DELETE /api/email-accounts/{id}
GET    /api/settings                 PATCH /api/settings
POST   /api/settings/logo            DELETE /api/settings/logo   GET /api/branding/logo
GET    /api/admin/status             POST /api/admin/test-ai     POST /api/admin/oauth-client
POST   /api/workspaces               DELETE /api/workspaces/{id}
```

Errors are returned as `{"error": {"message": "...", "code": "..."}}` with
user-safe messages; stack traces are only written to the server log.

---

## 6. Tests

```bash
pytest -q
```

* `tests/conftest.py` points all data paths at a temporary directory and builds
  the sample knowledge base from `tests/fixtures` (plus a generated PDF), so tests
  never touch — or depend on — the real knowledge base, database or tokens.
* `tests/test_api.py` drives the HTTP API end to end with an in-memory Gmail
  (`tests/fake_gmail.py`): upload/search/reprocess/delete (verifying the index),
  workspace isolation, settings validation, OAuth flow (PKCE, state replay),
  email → draft → edit → approve → human send → *Sent*, reject, Gmail-side
  deletion, token expiry/reconnect, disconnect/revoke, AI outage handling, CSRF
  and error hygiene.
* `tests/test_grounding.py` covers correspondence splitting, chunk sizing, thread
  context, citations, the exact-value check and follow-up questions.
* `python scripts/evaluate_kb.py` checks answer accuracy on the live Knowledge Base
  with the real AI service (direct, exact-value, multi-document, not-in-KB, edge
  and follow-up questions), printing question → excerpts → answer → sources.
* Remaining tests cover the AI client, ingestion/cleaning, hybrid retrieval and
  thresholds, index rebuild detection, workflow routing (one-call analysis,
  automated-mail skipping) and Gmail draft safety.

---

## 7. Security notes

* Secrets (AI key, Google client secret, OAuth tokens) stay on the server; OAuth
  tokens are encrypted at rest and never returned by the API.
* No CORS; state-changing requests need a custom header and matching origin.
* Content Security Policy blocks inline scripts; all email content is rendered as text.
* Workspaces separate data, but the app has no end-user login. Run it on a
  trusted network or behind your organisation's SSO/reverse proxy, and set
  `ADMIN_TOKEN` for any non-local deployment.
