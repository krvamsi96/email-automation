"""
Intelligent Email Copilot - Web API and frontend server.

The browser only talks to this API. Gmail, the knowledge store and the AI
service are called from the backend; no credentials are ever sent to the browser.
"""

import asyncio
import hmac
import logging
from functools import partial
from pathlib import Path
from typing import Any, Callable, Dict
from urllib.parse import quote, urlparse

from aiohttp import web

from app.config import Config
from app.rag.vector_store import DEFAULT_WORKSPACE_ID, get_vector_store_manager
from app.llm.client import llm_client
from app.services import activity
from app.services.ai_settings import ai_settings_service
from app.services.drafts import draft_service
from app.services.errors import NotFound, ServiceError
from app.services.gmail_accounts import CALLBACK_PATH, gmail_accounts
from app.services.knowledge import knowledge_service
from app.services.knowledge_test import knowledge_test_service
from app.services.email_test import email_test_service
from app.services.processing import processing_service
from app.services.workspaces import LOGO_TYPES, TONES, workspace_service

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent
STATIC_DIR = WEB_DIR / "static"
LOOPBACK = {"127.0.0.1", "::1", "localhost"}
CSRF_HEADER = "X-Requested-With"
AUTO_CHECK_TASK = web.AppKey("auto_check_task", asyncio.Task)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
async def run(fn: Callable, *args, **kwargs):
    """Runs blocking service code in a worker thread."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(fn, *args, **kwargs))


def ok(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status)


def workspace_id(request: web.Request) -> str:
    ws = request.headers.get("X-Workspace-Id") or request.query.get("ws") or DEFAULT_WORKSPACE_ID
    if not workspace_service.exists(ws):
        raise NotFound("Workspace not found. Please reload the page.", code="workspace_missing")
    return ws


def is_admin(request: web.Request) -> bool:
    if Config.ADMIN_TOKEN:
        supplied = request.headers.get("X-Admin-Token", "")
        return bool(supplied) and hmac.compare_digest(supplied, Config.ADMIN_TOKEN)
    return (request.remote or "") in LOOPBACK


def require_admin(request: web.Request) -> None:
    if not is_admin(request):
        raise ServiceError("Administrator access is required.", code="forbidden", status=403)


async def json_body(request: web.Request) -> Dict[str, Any]:
    if not request.can_read_body:
        return {}
    try:
        data = await request.json()
    except Exception:
        raise ServiceError("The request could not be read.")
    if not isinstance(data, dict):
        raise ServiceError("The request could not be read.")
    return data


def request_base_url(request: web.Request) -> str:
    return f"{request.scheme}://{request.host}"


# --------------------------------------------------------------------------
# middleware
# --------------------------------------------------------------------------
@web.middleware
async def error_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except ServiceError as e:
        return ok({"error": {"message": e.message, "code": e.code}}, status=e.status)
    except web.HTTPException as e:
        if request.path.startswith("/api/") and e.status >= 400:
            message = "Not found." if e.status == 404 else e.reason
            return ok({"error": {"message": message, "code": str(e.status)}}, status=e.status)
        raise
    except Exception:
        logger.exception("Unhandled error on %s %s", request.method, request.path)
        return ok({"error": {"message": "Something went wrong. Please try again.", "code": "internal"}}, status=500)


@web.middleware
async def security_middleware(request: web.Request, handler):
    # CSRF protection: state-changing API calls must come from this app's own pages.
    if request.path.startswith("/api/") and request.method not in ("GET", "HEAD", "OPTIONS"):
        if request.headers.get(CSRF_HEADER) != "copilot":
            return ok({"error": {"message": "Request blocked.", "code": "csrf"}}, status=403)
        origin = request.headers.get("Origin")
        if origin and urlparse(origin).netloc != request.host:
            return ok({"error": {"message": "Request blocked.", "code": "csrf"}}, status=403)

    response = await handler(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    if request.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


# --------------------------------------------------------------------------
# app / workspace
# --------------------------------------------------------------------------
async def api_app(request: web.Request) -> web.Response:
    ws = workspace_id(request)
    ws_row = await run(workspace_service.get, ws)
    admin = is_admin(request)
    return ok({
        "workspace": {"id": ws, "name": ws_row["name"]},
        "workspaces": await run(workspace_service.list),
        "branding": ws_row["settings"]["branding"],
        "is_admin": admin,
        "admin_token_required": bool(Config.ADMIN_TOKEN),
        "gmail_signin_available": gmail_accounts.client_status()["configured"],
        "ai_available": await run(llm_client.is_configured),
        "ai_provider": (await run(llm_client.active))["label"],
        "knowledge": {
            "types": knowledge_service.supported_types(),
            "extensions": knowledge_service.supported_extensions(),
            "max_upload_mb": Config.MAX_UPLOAD_MB,
        },
        "tones": TONES,
    })


async def api_workspaces_create(request: web.Request) -> web.Response:
    require_admin(request)
    data = await json_body(request)
    return ok(await run(workspace_service.create, data.get("name", "")), status=201)


async def api_workspaces_delete(request: web.Request) -> web.Response:
    require_admin(request)
    await run(workspace_service.delete, request.match_info["id"])
    return ok({"deleted": True})


# --------------------------------------------------------------------------
# dashboard / activity
# --------------------------------------------------------------------------
async def api_dashboard(request: web.Request) -> web.Response:
    ws = workspace_id(request)
    accounts = await run(gmail_accounts.list, ws)
    return ok({
        "emails": await run(processing_service.stats, ws),
        "drafts": await run(draft_service.stats, ws),
        "documents": await run(knowledge_service.stats, ws),
        "accounts": {
            "total": len(accounts),
            "connected": sum(1 for a in accounts if a["status"] == "connected"),
            "needs_attention": sum(1 for a in accounts if a["status"] != "connected"),
            "last_checked_at": max((a["last_checked_at"] or "" for a in accounts), default="") or None,
        },
        "job": processing_service.current_job(ws),
        "activity": await run(activity.recent, ws, 12),
    })


async def api_activity(request: web.Request) -> web.Response:
    ws = workspace_id(request)
    try:
        limit = int(request.query.get("limit", "20"))
    except ValueError:
        limit = 20
    return ok({"items": await run(activity.recent, ws, limit)})


# --------------------------------------------------------------------------
# emails
# --------------------------------------------------------------------------
async def api_emails(request: web.Request) -> web.Response:
    ws = workspace_id(request)
    items = await run(
        processing_service.list_emails, ws,
        request.query.get("status", ""), request.query.get("q", "").strip()[:200],
    )
    return ok({"items": items, "job": processing_service.current_job(ws)})


async def api_email_detail(request: web.Request) -> web.Response:
    ws = workspace_id(request)
    return ok(await run(processing_service.get_email, ws, request.match_info["id"]))


async def api_email_process(request: web.Request) -> web.Response:
    ws = workspace_id(request)
    result = await run(processing_service.reprocess, ws, request.match_info["id"])
    return ok(result)


async def api_check_start(request: web.Request) -> web.Response:
    ws = workspace_id(request)
    return ok(processing_service.start_check(ws), status=202)


async def api_check_status(request: web.Request) -> web.Response:
    ws = workspace_id(request)
    return ok({"job": processing_service.current_job(ws)})


# --------------------------------------------------------------------------
# knowledge base
# --------------------------------------------------------------------------
async def api_documents(request: web.Request) -> web.Response:
    ws = workspace_id(request)
    return ok({"items": await run(knowledge_service.list, ws), "stats": await run(knowledge_service.stats, ws)})


async def api_documents_upload(request: web.Request) -> web.Response:
    ws = workspace_id(request)
    try:
        reader = await request.multipart()
    except Exception:
        raise ServiceError("Please choose one or more files to upload.")
    limit = Config.MAX_UPLOAD_MB * 1024 * 1024
    uploaded, failed = [], []
    count = 0
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name != "files" or not part.filename:
            continue
        count += 1
        if count > 20:
            failed.append({"filename": part.filename, "error": "Upload up to 20 files at a time."})
            continue
        data = bytearray()
        too_large = False
        while True:
            chunk = await part.read_chunk(256 * 1024)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > limit:
                too_large = True
                break
        if too_large:
            failed.append({"filename": part.filename, "error": f"File is larger than {Config.MAX_UPLOAD_MB} MB."})
            continue
        try:
            uploaded.append(await run(knowledge_service.upload, ws, part.filename, bytes(data)))
        except ServiceError as e:
            failed.append({"filename": part.filename, "error": e.message})
    if not uploaded and not failed:
        raise ServiceError("Please choose one or more files to upload.")
    status = 201 if uploaded else 400
    return ok({"uploaded": uploaded, "failed": failed}, status=status)


async def api_document_delete(request: web.Request) -> web.Response:
    ws = workspace_id(request)
    return ok(await run(knowledge_service.delete, ws, request.match_info["id"]))


async def api_document_reprocess(request: web.Request) -> web.Response:
    ws = workspace_id(request)
    return ok(await run(knowledge_service.reprocess, ws, request.match_info["id"]))


async def api_documents_reprocess_all(request: web.Request) -> web.Response:
    ws = workspace_id(request)
    return ok({"queued": await run(knowledge_service.reprocess_all, ws)})


async def api_document_detail(request: web.Request) -> web.Response:
    ws = workspace_id(request)
    return ok(await run(knowledge_service.details, ws, request.match_info["id"]))


async def api_knowledge_ask(request: web.Request) -> web.Response:
    """Knowledge Base Test: answers a question from the Knowledge Base. Gmail is not involved."""
    ws = workspace_id(request)
    data = await json_body(request)
    return ok(await run(knowledge_test_service.ask, ws, data.get("question"), data.get("follow_up_to")), status=202)


async def api_knowledge_tests(request: web.Request) -> web.Response:
    ws = workspace_id(request)
    return ok({"items": await run(knowledge_test_service.history, ws)})


async def api_knowledge_test_detail(request: web.Request) -> web.Response:
    ws = workspace_id(request)
    return ok(await run(knowledge_test_service.get, ws, request.match_info["id"]))


async def api_knowledge_tests_clear(request: web.Request) -> web.Response:
    ws = workspace_id(request)
    return ok({"deleted": await run(knowledge_test_service.clear_history, ws)})


async def api_email_test(request: web.Request) -> web.Response:
    """Email Test: typed-in email -> full AI workflow -> reply preview. No Gmail."""
    ws = workspace_id(request)
    data = await json_body(request)
    return ok(await run(email_test_service.start, ws, data.get("sender"), data.get("subject"), data.get("body")), status=202)


async def api_email_test_detail(request: web.Request) -> web.Response:
    ws = workspace_id(request)
    return ok(email_test_service.get(ws, request.match_info["id"]))


async def api_knowledge_search(request: web.Request) -> web.Response:
    ws = workspace_id(request)
    return ok(await run(knowledge_service.search, ws, request.query.get("q", "")))


# --------------------------------------------------------------------------
# drafts / approvals
# --------------------------------------------------------------------------
async def api_drafts(request: web.Request) -> web.Response:
    ws = workspace_id(request)
    return ok({
        "items": await run(draft_service.list, ws, request.query.get("status", "")),
        "stats": await run(draft_service.stats, ws),
    })


async def api_draft_detail(request: web.Request) -> web.Response:
    ws = workspace_id(request)
    return ok(await run(draft_service.get, ws, request.match_info["id"]))


async def api_draft_update(request: web.Request) -> web.Response:
    ws = workspace_id(request)
    data = await json_body(request)
    return ok(await run(draft_service.update_body, ws, request.match_info["id"], data.get("body")))


async def api_draft_approve(request: web.Request) -> web.Response:
    ws = workspace_id(request)
    data = await json_body(request)
    return ok(await run(draft_service.approve, ws, request.match_info["id"], data.get("body")))


async def api_draft_reject(request: web.Request) -> web.Response:
    ws = workspace_id(request)
    data = await json_body(request)
    return ok(await run(draft_service.reject, ws, request.match_info["id"], str(data.get("reason", ""))))


async def api_draft_retry(request: web.Request) -> web.Response:
    ws = workspace_id(request)
    return ok(await run(draft_service.retry, ws, request.match_info["id"]))


async def api_drafts_sync(request: web.Request) -> web.Response:
    ws = workspace_id(request)
    return ok(await run(draft_service.sync_status, ws))


# --------------------------------------------------------------------------
# email accounts
# --------------------------------------------------------------------------
async def api_accounts(request: web.Request) -> web.Response:
    ws = workspace_id(request)
    return ok({
        "items": await run(gmail_accounts.list, ws),
        "signin_available": gmail_accounts.client_status()["configured"],
    })


async def api_account_connect(request: web.Request) -> web.Response:
    ws = workspace_id(request)
    url = await run(gmail_accounts.start_oauth, ws, request_base_url(request))
    return ok({"auth_url": url})


async def api_oauth_callback(request: web.Request) -> web.Response:
    """Google redirects the browser here after the user grants (or denies) access."""
    params = request.query
    if params.get("error"):
        reason = "cancelled" if params.get("error") == "access_denied" else "failed"
        raise web.HTTPFound(f"/#/accounts?oauth={reason}")
    try:
        account = await run(gmail_accounts.complete_oauth, params.get("state", ""), params.get("code", ""))
    except ServiceError as e:
        raise web.HTTPFound(f"/#/accounts?oauth=failed&message={quote(e.message)}")
    except Exception:
        # The browser is mid-redirect from Google: always return to the app, never show a raw error.
        logger.exception("Gmail sign-in callback failed")
        raise web.HTTPFound("/#/accounts?oauth=failed&message=" + quote("Google sign-in could not be completed. Please try again."))
    raise web.HTTPFound(f"/#/accounts?oauth=connected&account={quote(account['email'])}")


async def api_account_test(request: web.Request) -> web.Response:
    ws = workspace_id(request)
    return ok(await run(gmail_accounts.test, ws, request.match_info["id"]))


async def api_account_delete(request: web.Request) -> web.Response:
    ws = workspace_id(request)
    return ok(await run(gmail_accounts.disconnect, ws, request.match_info["id"]))


# --------------------------------------------------------------------------
# settings / branding
# --------------------------------------------------------------------------
async def api_settings(request: web.Request) -> web.Response:
    ws = workspace_id(request)
    settings = await run(workspace_service.settings, ws)
    if not is_admin(request):
        settings.pop("advanced", None)
    return ok({"settings": settings})


async def api_settings_update(request: web.Request) -> web.Response:
    ws = workspace_id(request)
    data = await json_body(request)
    settings = await run(workspace_service.update_settings, ws, data, is_admin(request))
    if not is_admin(request):
        settings.pop("advanced", None)
    return ok({"settings": settings})


async def _read_single_file(request: web.Request, field: str, max_bytes: int):
    reader = await request.multipart()
    while True:
        part = await reader.next()
        if part is None:
            raise ServiceError("Please choose a file.")
        if part.name == field and part.filename:
            data = bytearray()
            while True:
                chunk = await part.read_chunk(64 * 1024)
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > max_bytes:
                    raise ServiceError("The file is too large.")
            return part.filename, bytes(data)


async def api_logo_upload(request: web.Request) -> web.Response:
    ws = workspace_id(request)
    filename, data = await _read_single_file(request, "logo", 2 * 1024 * 1024 + 1)
    return ok({"settings": await run(workspace_service.save_logo, ws, filename, data)})


async def api_logo_delete(request: web.Request) -> web.Response:
    ws = workspace_id(request)
    return ok({"settings": await run(workspace_service.remove_logo, ws)})


async def api_logo(request: web.Request) -> web.StreamResponse:
    ws = workspace_id(request)
    path = await run(workspace_service.logo_path, ws)
    if not path:
        raise NotFound("No logo.")
    resp = web.FileResponse(path, headers={
        "Content-Type": LOGO_TYPES.get(path.suffix.lower(), "application/octet-stream"),
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
        "Cache-Control": "no-cache",
    })
    return resp


# --------------------------------------------------------------------------
# administration
# --------------------------------------------------------------------------
async def api_admin_status(request: web.Request) -> web.Response:
    require_admin(request)
    ws = workspace_id(request)
    oauth = gmail_accounts.client_status()
    return ok({
        "ai_service": await run(ai_settings_service.status),
        "knowledge_storage": {
            "indexed_sections": await run(get_vector_store_manager(ws).count),
            "documents": (await run(knowledge_service.stats, ws))["total"],
            "search_model": Config.EMBEDDING_MODEL,
        },
        "gmail_signin": {
            **oauth,
            "redirect_uri": gmail_accounts.redirect_uri(request_base_url(request)),
        },
        "security": {
            "admin_token_enabled": bool(Config.ADMIN_TOKEN),
            "token_encryption_key": "environment" if Config.APP_SECRET_KEY else "local key file",
        },
    })


async def api_admin_test_ai(request: web.Request) -> web.Response:
    require_admin(request)
    data = await json_body(request)
    return ok(await run(ai_settings_service.test, data.get("provider"), data.get("model")))


async def api_admin_ai_update(request: web.Request) -> web.Response:
    require_admin(request)
    data = await json_body(request)
    return ok(await run(ai_settings_service.update, data))


async def api_admin_ai_models(request: web.Request) -> web.Response:
    require_admin(request)
    refresh = request.query.get("refresh") == "1"
    return ok(await run(ai_settings_service.models, request.match_info["provider"], refresh))


async def api_admin_oauth_client(request: web.Request) -> web.Response:
    require_admin(request)
    _, data = await _read_single_file(request, "client", 64 * 1024)
    return ok(await run(gmail_accounts.save_client_config, data))


async def api_admin_verify(request: web.Request) -> web.Response:
    return ok({"is_admin": is_admin(request)})


# --------------------------------------------------------------------------
# frontend
# --------------------------------------------------------------------------
INDEX_CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)


async def index_handler(request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC_DIR / "index.html", headers={
        "Content-Security-Policy": INDEX_CSP,
        "Cache-Control": "no-cache",
    })


# --------------------------------------------------------------------------
# background tasks
# --------------------------------------------------------------------------
async def auto_check_loop(app: web.Application):
    """Starts inbox checks for workspaces that enabled automatic checking."""
    last_run: Dict[str, float] = {}
    loop = asyncio.get_running_loop()
    while True:
        try:
            await asyncio.sleep(30)
            for ws in await run(workspace_service.list):
                minutes = (await run(workspace_service.settings, ws["id"]))["email"]["auto_check_minutes"]
                if not minutes:
                    continue
                now = loop.time()
                if now - last_run.get(ws["id"], 0) < minutes * 60 or processing_service.is_running(ws["id"]):
                    continue
                if not await run(gmail_accounts.list, ws["id"]):
                    continue
                last_run[ws["id"]] = now
                try:
                    processing_service.start_check(ws["id"], trigger="automatic")
                except ServiceError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Automatic email check loop error")


async def on_startup(app: web.Application):
    await run(startup_tasks)
    app[AUTO_CHECK_TASK] = asyncio.create_task(auto_check_loop(app))


async def on_cleanup(app: web.Application):
    task = app.get(AUTO_CHECK_TASK)
    if task:
        task.cancel()


def warm_up():
    """Loads the embedding and re-ranking models and keyword indexes so the first question is answered without delay."""
    try:
        from app.rag.embeddings import get_embeddings
        from app.rag.reranker import get_reranker
        from app.rag.retriever import get_retriever
        get_embeddings().warm_up()
        get_reranker().warm_up()
        for ws in workspace_service.list():
            get_retriever(ws["id"]).lexical_index()
        logger.info("Knowledge search is ready (re-ranking %s)",
                    "on" if get_reranker().available else "unavailable: hybrid ranking only")
    except Exception:
        logger.exception("Warm-up failed; the first knowledge search will load the model instead")


def startup_tasks():
    workspace_service.ensure_default()
    get_vector_store_manager(DEFAULT_WORKSPACE_ID).drop_legacy_collection()
    for ws in workspace_service.list():
        # Knowledge built with another embedding model or an older pipeline is rebuilt.
        knowledge_service.ensure_index_current(ws["id"])
        knowledge_service.sync_folder(ws["id"])
    knowledge_service.resume_pending()
    # Emails interrupted mid-processing by a restart go back to human review.
    from app.store.db import get_db
    get_db().execute(
        "UPDATE emails SET status = 'needs_attention', "
        "status_message = 'Processing was interrupted. Use Process again or reply personally.' "
        "WHERE status IN ('processing', 'new')"
    )
    knowledge_test_service.recover_interrupted()
    # Gmail is optional: adopting an old command-line sign-in may need the network,
    # so it runs in the background and can never delay or block startup.
    import threading
    threading.Thread(target=gmail_accounts.import_legacy_token, args=(DEFAULT_WORKSPACE_ID,),
                     name="legacy-gmail-import", daemon=True).start()
    threading.Thread(target=warm_up, name="warm-up", daemon=True).start()


def create_app() -> web.Application:
    app = web.Application(middlewares=[security_middleware, error_middleware], client_max_size=4 * 1024 * 1024)
    r = app.router
    r.add_get("/api/app", api_app)
    r.add_post("/api/workspaces", api_workspaces_create)
    r.add_delete("/api/workspaces/{id}", api_workspaces_delete)

    r.add_get("/api/dashboard/stats", api_dashboard)
    r.add_get("/api/activity", api_activity)

    r.add_get("/api/emails", api_emails)
    r.add_post("/api/emails/check", api_check_start)
    r.add_get("/api/emails/check", api_check_status)
    r.add_get("/api/emails/{id}", api_email_detail)
    r.add_post("/api/emails/{id}/process", api_email_process)

    r.add_get("/api/documents", api_documents)
    r.add_post("/api/documents/upload", api_documents_upload)
    r.add_post("/api/documents/reprocess-all", api_documents_reprocess_all)
    r.add_get("/api/documents/{id}", api_document_detail)
    r.add_delete("/api/documents/{id}", api_document_delete)
    r.add_post("/api/documents/{id}/reprocess", api_document_reprocess)
    r.add_get("/api/knowledge/search", api_knowledge_search)
    r.add_post("/api/email-test", api_email_test)
    r.add_get("/api/email-test/{id}", api_email_test_detail)
    r.add_post("/api/knowledge/ask", api_knowledge_ask)
    r.add_get("/api/knowledge/tests", api_knowledge_tests)
    r.add_delete("/api/knowledge/tests", api_knowledge_tests_clear)
    r.add_get("/api/knowledge/tests/{id}", api_knowledge_test_detail)

    r.add_get("/api/drafts", api_drafts)
    r.add_post("/api/drafts/sync", api_drafts_sync)
    r.add_get("/api/drafts/{id}", api_draft_detail)
    r.add_put("/api/drafts/{id}", api_draft_update)
    r.add_post("/api/drafts/{id}/approve", api_draft_approve)
    r.add_post("/api/drafts/{id}/reject", api_draft_reject)
    r.add_post("/api/drafts/{id}/retry", api_draft_retry)

    r.add_get("/api/email-accounts", api_accounts)
    r.add_post("/api/email-accounts/connect", api_account_connect)
    r.add_get(CALLBACK_PATH, api_oauth_callback)
    r.add_post("/api/email-accounts/{id}/test", api_account_test)
    r.add_delete("/api/email-accounts/{id}", api_account_delete)

    r.add_get("/api/settings", api_settings)
    r.add_patch("/api/settings", api_settings_update)
    r.add_post("/api/settings/logo", api_logo_upload)
    r.add_delete("/api/settings/logo", api_logo_delete)
    r.add_get("/api/branding/logo", api_logo)

    r.add_get("/api/admin/status", api_admin_status)
    r.add_post("/api/admin/test-ai", api_admin_test_ai)
    r.add_put("/api/admin/ai", api_admin_ai_update)
    r.add_get("/api/admin/ai/models/{provider}", api_admin_ai_models)
    r.add_post("/api/admin/oauth-client", api_admin_oauth_client)
    r.add_get("/api/admin/verify", api_admin_verify)

    r.add_get("/", index_handler)
    r.add_static("/static/", path=STATIC_DIR, name="static")

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def run_server(host: str = "127.0.0.1", port: int = 8000):
    """Starts the web application."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    print("\n====================================================================")
    print(f"  {Config.PRODUCT_NAME}")
    print(f"  Open http://{host}:{port} in your browser")
    print("====================================================================\n")
    web.run_app(create_app(), host=host, port=port, print=None)


if __name__ == "__main__":
    run_server()
