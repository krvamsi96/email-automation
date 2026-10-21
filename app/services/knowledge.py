"""
Knowledge Base service: document upload, indexing, deletion, reprocessing and search.

Documents are processed in a single background worker so the UI never blocks.
The processing status stored in the database is the real state of the index.
"""

import logging
import re
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import Config
from app.rag.ingestion import SUPPORTED_EXTENSIONS, DocumentLoadError, IngestionPipeline
from app.rag.retriever import get_retriever
from app.rag.vector_store import get_vector_store_manager
from app.services import activity
from app.services.errors import Conflict, NotFound, ServiceError
from app.services.workspaces import workspace_service
from app.store.db import get_db, new_id, now_iso

logger = logging.getLogger(__name__)

DOC_COLUMNS = "id, filename, file_type, size_bytes, status, status_message, chunk_count, uploaded_at, processed_at"


def sanitize_filename(name: str) -> str:
    name = unicodedata.normalize("NFC", Path(str(name or "")).name).strip()
    name = re.sub(r"[\x00-\x1f<>:\"/\\|?*]", "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > 120:
        stem, ext = Path(name).stem, Path(name).suffix
        name = stem[: 120 - len(ext)] + ext
    return name


class KnowledgeService:

    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="knowledge")
        self._pending: set = set()
        self._pending_lock = threading.Lock()

    # --- helpers ---------------------------------------------------------
    def _pipeline(self, workspace_id: str) -> IngestionPipeline:
        return IngestionPipeline(
            knowledge_base_path=str(workspace_service.documents_dir(workspace_id)),
            manager=get_vector_store_manager(workspace_id),
        )

    def _get_row(self, workspace_id: str, doc_id: str) -> Dict[str, Any]:
        row = get_db().fetchone(
            f"SELECT {DOC_COLUMNS} FROM documents WHERE id = ? AND workspace_id = ?",
            (doc_id, workspace_id),
        )
        if not row:
            raise NotFound("Document not found. It may have been deleted.")
        return row

    def _path(self, workspace_id: str, filename: str) -> Path:
        return workspace_service.documents_dir(workspace_id) / filename

    @staticmethod
    def supported_types() -> List[str]:
        return sorted(set(SUPPORTED_EXTENSIONS.values()))

    @staticmethod
    def supported_extensions() -> List[str]:
        return sorted(SUPPORTED_EXTENSIONS.keys())

    # --- queries ---------------------------------------------------------
    def list(self, workspace_id: str) -> List[Dict[str, Any]]:
        return get_db().fetchall(
            f"SELECT {DOC_COLUMNS} FROM documents WHERE workspace_id = ? ORDER BY uploaded_at DESC, filename",
            (workspace_id,),
        )

    def get(self, workspace_id: str, doc_id: str) -> Dict[str, Any]:
        return self._get_row(workspace_id, doc_id)

    def details(self, workspace_id: str, doc_id: str) -> Dict[str, Any]:
        """Document record plus a preview of the knowledge created from it."""
        doc = self._get_row(workspace_id, doc_id)
        preview: List[Dict[str, Any]] = []
        if doc["status"] == "ready":
            for chunk in get_vector_store_manager(workspace_id).document_chunks(doc_id)[:3]:
                text = re.sub(r"\s+", " ", chunk["text"]).strip()
                preview.append({"text": text[:500] + ("…" if len(text) > 500 else ""), "page": chunk["metadata"].get("page")})
        return {**doc, "preview": preview}

    def stats(self, workspace_id: str) -> Dict[str, int]:
        rows = get_db().fetchall(
            "SELECT status, COUNT(*) AS n FROM documents WHERE workspace_id = ? GROUP BY status",
            (workspace_id,),
        )
        by = {r["status"]: r["n"] for r in rows}
        return {
            "total": sum(by.values()),
            "ready": by.get("ready", 0),
            "processing": by.get("processing", 0),
            "failed": by.get("failed", 0),
        }

    # --- registration / sync --------------------------------------------
    def sync_folder(self, workspace_id: str, schedule: bool = True) -> None:
        """
        Registers files placed directly in the workspace folder and removes
        records (and indexed knowledge) for files that no longer exist.
        """
        folder = workspace_service.documents_dir(workspace_id)
        db = get_db()
        known = {r["filename"]: r for r in db.fetchall(
            "SELECT id, filename, status FROM documents WHERE workspace_id = ?", (workspace_id,))}

        for path in sorted(folder.iterdir()):
            if not path.is_file() or path.name.startswith("."):
                continue
            if path.suffix.lower() not in SUPPORTED_EXTENSIONS or path.name in known:
                continue
            doc_id = new_id()
            db.insert("documents", {
                "id": doc_id,
                "workspace_id": workspace_id,
                "filename": path.name,
                "file_type": SUPPORTED_EXTENSIONS[path.suffix.lower()],
                "size_bytes": path.stat().st_size,
                "status": "processing",
                "status_message": "",
                "chunk_count": 0,
                "uploaded_at": now_iso(),
            })
            if schedule:
                self.schedule(workspace_id, doc_id)

        # Compare with the directory listing rather than Path.exists(): on Windows exists() is
        # False for paths over MAX_PATH (260 characters) and on transient I/O errors, which
        # would delete the record and its indexed knowledge although the file is still there.
        present = {p.name for p in folder.iterdir()}
        for filename, row in known.items():
            if filename not in present:
                get_vector_store_manager(workspace_id).delete_document(row["id"])
                db.execute("DELETE FROM documents WHERE id = ?", (row["id"],))

    def ensure_index_current(self, workspace_id: str) -> bool:
        """
        Rebuilds the workspace's knowledge when it was indexed with another embedding
        model or an older indexing pipeline (their vectors are not comparable).
        Returns True when a rebuild was scheduled.
        """
        manager = get_vector_store_manager(workspace_id)
        if manager.index_is_current():
            return False
        rows = self.list(workspace_id)
        logger.info("Knowledge index of workspace %s is outdated; rebuilding %d document(s)", workspace_id, len(rows))
        manager.reset()
        _ = manager.collection  # recreate with current metadata so the rebuild is recorded as current
        for row in rows:
            get_db().update("documents", row["id"], {"status": "processing", "status_message": "Updating knowledge…"})
        return bool(rows)

    def resume_pending(self) -> None:
        """Re-queues documents left in 'processing' (e.g. after a restart)."""
        for row in get_db().fetchall("SELECT id, workspace_id FROM documents WHERE status = 'processing'"):
            self.schedule(row["workspace_id"], row["id"])

    # --- processing ------------------------------------------------------
    def schedule(self, workspace_id: str, doc_id: str) -> bool:
        with self._pending_lock:
            if doc_id in self._pending:
                return False
            self._pending.add(doc_id)
        self._executor.submit(self._process_safe, workspace_id, doc_id)
        return True

    def is_pending(self, doc_id: str) -> bool:
        with self._pending_lock:
            return doc_id in self._pending

    def _process_safe(self, workspace_id: str, doc_id: str) -> None:
        try:
            self.process(workspace_id, doc_id)
        except Exception:
            logger.exception("Unexpected error processing document %s", doc_id)
        finally:
            with self._pending_lock:
                self._pending.discard(doc_id)

    def process(self, workspace_id: str, doc_id: str) -> Optional[int]:
        """Indexes a document synchronously and records the real outcome."""
        db = get_db()
        row = db.fetchone("SELECT * FROM documents WHERE id = ? AND workspace_id = ?", (doc_id, workspace_id))
        if not row:
            return None  # deleted while queued
        path = self._path(workspace_id, row["filename"])
        db.update("documents", doc_id, {"status": "processing", "status_message": "Processing document…"})
        try:
            if not path.exists():
                raise DocumentLoadError("The original file is missing. Please upload it again.")
            pipeline = self._pipeline(workspace_id)
            pieces = pipeline.chunk_file(path, doc_id, row["filename"])
            if not pieces:
                raise DocumentLoadError(
                    "No readable text was found in this document. Scanned images are not supported.")
            db.update("documents", doc_id, {"status_message": "Preparing knowledge…"})
            chunks = pipeline.store_chunks(doc_id, pieces)
        except DocumentLoadError as e:
            get_vector_store_manager(workspace_id).delete_document(doc_id)
            if db.fetchone("SELECT id FROM documents WHERE id = ?", (doc_id,)):
                db.update("documents", doc_id, {
                    "status": "failed", "status_message": str(e),
                    "chunk_count": 0, "processed_at": now_iso(),
                })
                activity.log(workspace_id, "document_failed", f"Processing failed: {row['filename']}", "document", doc_id)
            return None
        except Exception:
            logger.exception("Indexing failed for %s", row["filename"])
            get_vector_store_manager(workspace_id).delete_document(doc_id)
            if db.fetchone("SELECT id FROM documents WHERE id = ?", (doc_id,)):
                db.update("documents", doc_id, {
                    "status": "failed",
                    "status_message": "The document could not be processed. Please try again.",
                    "chunk_count": 0, "processed_at": now_iso(),
                })
                activity.log(workspace_id, "document_failed", f"Processing failed: {row['filename']}", "document", doc_id)
            return None

        # The document may have been deleted while it was being indexed.
        if not db.fetchone("SELECT id FROM documents WHERE id = ?", (doc_id,)):
            get_vector_store_manager(workspace_id).delete_document(doc_id)
            return None
        db.update("documents", doc_id, {
            "status": "ready", "status_message": "", "chunk_count": chunks, "processed_at": now_iso(),
        })
        activity.log(workspace_id, "document_ready", f"Knowledge updated: {row['filename']}", "document", doc_id)
        return chunks

    # --- commands --------------------------------------------------------
    def upload(self, workspace_id: str, filename: str, data: bytes) -> Dict[str, Any]:
        name = sanitize_filename(filename)
        ext = Path(name).suffix.lower()
        if not name or not Path(name).stem:
            raise ServiceError("The file name is not valid.")
        if ext not in SUPPORTED_EXTENSIONS:
            raise ServiceError(
                f"'{name}' is not a supported file type. Supported: {', '.join(self.supported_types())}.",
                code="unsupported_type",
            )
        if len(data) == 0:
            raise ServiceError(f"'{name}' is empty.")
        if len(data) > Config.MAX_UPLOAD_MB * 1024 * 1024:
            raise ServiceError(f"'{name}' is larger than {Config.MAX_UPLOAD_MB} MB.", code="too_large")

        db = get_db()
        if db.fetchone("SELECT id FROM documents WHERE workspace_id = ? AND filename = ?", (workspace_id, name)):
            raise Conflict(f"A document named '{name}' already exists. Delete it first to replace it.")

        path = self._path(workspace_id, name)
        if path.exists():
            raise Conflict(f"A document named '{name}' already exists. Delete it first to replace it.")
        path.write_bytes(data)

        doc_id = new_id()
        try:
            db.insert("documents", {
                "id": doc_id,
                "workspace_id": workspace_id,
                "filename": name,
                "file_type": SUPPORTED_EXTENSIONS[ext],
                "size_bytes": len(data),
                "status": "processing",
                "status_message": "",
                "chunk_count": 0,
                "uploaded_at": now_iso(),
            })
        except Exception:
            path.unlink(missing_ok=True)
            raise Conflict(f"A document named '{name}' already exists.")
        activity.log(workspace_id, "document_uploaded", f"Document uploaded: {name}", "document", doc_id)
        self.schedule(workspace_id, doc_id)
        return self._get_row(workspace_id, doc_id)

    def reprocess(self, workspace_id: str, doc_id: str) -> Dict[str, Any]:
        self._get_row(workspace_id, doc_id)
        if self.is_pending(doc_id):
            raise Conflict("This document is already being processed.")
        get_db().update("documents", doc_id, {"status": "processing", "status_message": ""})
        self.schedule(workspace_id, doc_id)
        return self._get_row(workspace_id, doc_id)

    def delete(self, workspace_id: str, doc_id: str) -> Dict[str, Any]:
        """Removes the original file, its indexed knowledge and its record."""
        row = self._get_row(workspace_id, doc_id)
        manager = get_vector_store_manager(workspace_id)
        removed = manager.delete_document(doc_id)
        if manager.count_document(doc_id) != 0:
            raise ServiceError("The document's knowledge could not be fully removed. Please try again.", status=500)
        path = self._path(workspace_id, row["filename"])
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.exception("Could not delete file %s", path)
            raise ServiceError("The document file could not be removed. Please try again.", status=500)
        get_db().execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        activity.log(workspace_id, "document_deleted", f"Document deleted: {row['filename']}", "document", doc_id)
        return {"id": doc_id, "filename": row["filename"], "chunks_removed": removed}

    def reindex_all(self, workspace_id: str, reset: bool = False) -> int:
        """Synchronously (re)indexes every document in the workspace. Returns total chunks."""
        self.sync_folder(workspace_id, schedule=False)
        if reset:
            get_vector_store_manager(workspace_id).reset()
        total = 0
        for row in self.list(workspace_id):
            total += self.process(workspace_id, row["id"]) or 0
        return total

    def reprocess_all(self, workspace_id: str) -> int:
        count = 0
        for row in self.list(workspace_id):
            if not self.is_pending(row["id"]):
                get_db().update("documents", row["id"], {"status": "processing", "status_message": ""})
                self.schedule(workspace_id, row["id"])
                count += 1
        return count

    def search(self, workspace_id: str, query: str, limit: int = 8) -> Dict[str, Any]:
        query = (query or "").strip()
        if not query:
            raise ServiceError("Enter something to search for.")
        if len(query) > 500:
            raise ServiceError("Search text is too long.")
        threshold = workspace_service.settings(workspace_id)["advanced"]["min_relevance"]
        hits = get_retriever(workspace_id).search(query, top_k=max(limit * 2, 8), min_relevance=threshold)

        docs_by_id = {d["id"]: d for d in self.list(workspace_id)}
        grouped: Dict[str, Dict[str, Any]] = {}
        for hit in hits:
            chunk, score = hit.to_document(), hit.relevance
            doc_id = chunk.metadata.get("doc_id")
            doc = docs_by_id.get(doc_id)
            if not doc:
                continue  # knowledge from a document that is no longer registered
            entry = grouped.setdefault(doc_id, {
                "document_id": doc_id,
                "filename": doc["filename"],
                "file_type": doc["file_type"],
                "relevance": 0.0,
                "matches": [],
            })
            entry["relevance"] = max(entry["relevance"], round(score, 3))
            if len(entry["matches"]) < 3:
                snippet = re.sub(r"\s+", " ", chunk.page_content).strip()
                entry["matches"].append({
                    "text": snippet[:400] + ("…" if len(snippet) > 400 else ""),
                    "page": chunk.metadata.get("page"),
                    "relevance": round(score, 3),
                })
        results = sorted(grouped.values(), key=lambda r: r["relevance"], reverse=True)[:limit]
        return {"query": query, "total": len(results), "results": results}


knowledge_service = KnowledgeService()
