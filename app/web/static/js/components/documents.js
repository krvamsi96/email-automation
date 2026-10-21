// Document upload + list, shared by the Knowledge Base and Knowledge Base Test pages.
import { api } from "../api.js";
import { icon } from "../icons.js";
import { appState, changed, on } from "../store.js";
import {
  h, mount, loadingBlock, errorState, emptyState, statusBadge, formatDay, formatDateTime, formatBytes,
  actionMenu, confirmDialog, busy, notify, modal,
} from "../ui.js";

/**
 * Returns { el, uploadButton, reload } and calls onChange(stats) whenever the list changes.
 * `compact` hides size/uploaded columns for embedding in other pages.
 */
export function documentsPanel(ctx, { compact = false, onChange } = {}) {
  const k = appState.info.knowledge;
  const fileInput = h("input", { type: "file", multiple: true, accept: k.extensions.join(","), hidden: true });
  const uploadButton = h("button", { type: "button", class: "btn btn-primary", onclick: () => fileInput.click() }, icon("plus", 16), "Upload Documents");
  const uploadsEl = h("ul", { class: "uploads", hidden: true });
  const dropzone = h("div", { class: `dropzone${compact ? " dropzone-compact" : ""}` },
    h("div", { class: "dz-icon" }, icon("upload", 22)),
    h("div", { class: "dz-title" }, "Drag & drop documents here"),
    h("div", { style: "color:var(--text-3)" }, "or"),
    h("button", { type: "button", class: "btn", onclick: () => fileInput.click() }, "Browse Files"),
    h("div", { class: "dz-types" }, `${k.types.join(" • ")} · up to ${k.max_upload_mb} MB each · one or many files`),
  );
  const statsEl = h("span", { class: "chip", hidden: true });
  const listEl = h("div", {}, loadingBlock("Loading documents…"));

  const el = h("div", { class: "stack" },
    h("section", { class: "card" }, h("div", { class: "card-body" }, dropzone, uploadsEl, fileInput)),
    h("section", { class: "card" },
      h("div", { class: "card-head" }, h("h2", {}, "Knowledge Base Documents"), statsEl),
      listEl,
    ),
  );

  let docs = [];
  let pollStop = null;
  let seq = 0;
  async function reload() {
    const mine = ++seq;
    let data;
    try {
      data = await api.get("/api/documents");
    } catch (e) {
      if (ctx.alive() && mine === seq) mount(listEl, errorState(e.message, reload));
      return;
    }
    if (!ctx.alive() || mine !== seq) return;
    const before = new Map(docs.map((d) => [d.id, d.status]));
    docs = data.items;
    for (const d of docs) {
      const prev = before.get(d.id);
      if (prev === "processing" && d.status === "ready") notify.success("Document ready", `${d.filename} is now part of the Knowledge Base.`);
      if (prev === "processing" && d.status === "failed") notify.error("Unable to process this document", `${d.filename}: ${d.status_message}`);
    }
    statsEl.hidden = !data.stats.total;
    statsEl.textContent = `${data.stats.ready} ready of ${data.stats.total}`;
    draw();
    if (onChange) onChange(data.stats);
    const processing = docs.some((d) => d.status === "processing");
    if (processing && !pollStop) pollStop = ctx.interval(reload, 1500);
    if (!processing && pollStop) { pollStop(); pollStop = null; changed("documents"); }
  }

  function draw() {
    if (!docs.length) {
      mount(listEl, emptyState({
        iconName: "book", title: "No knowledge documents yet",
        text: "Upload your first document to start building your knowledge base.",
        action: h("button", { type: "button", class: "btn btn-primary", onclick: () => fileInput.click() }, icon("upload", 16), "Upload Document"),
      }));
      return;
    }
    const cols = compact ? ["Document", "Type", "Status", ""] : ["Document Name", "Type", "Size", "Uploaded", "Status", ""];
    mount(listEl, h("div", { class: "table-wrap" }, h("table", { class: "table responsive" },
      h("thead", {}, h("tr", {}, cols.map((c, i) => h("th", { class: i === cols.length - 1 ? "col-actions" : null },
        c || h("span", { class: "visually-hidden" }, "Actions"))))),
      h("tbody", {}, docs.map(row)),
    )));
  }

  function row(d) {
    const processing = d.status === "processing";
    const statusCell = h("td", { "data-label": "Status" },
      statusBadge(d.status),
      processing && d.status_message ? h("div", { class: "cell-sub" }, d.status_message) : null);
    return h("tr", {},
      h("td", { "data-label": "Name" },
        h("div", { style: "display:flex;gap:10px;align-items:center" },
          h("span", { style: "color:var(--text-3)" }, icon("file", 18)),
          h("div", { style: "min-width:0" },
            h("button", { type: "button", class: "link-btn cell-title truncate", style: "max-width:360px", onclick: () => showDetails(d) }, d.filename),
            d.status === "ready" ? h("div", { class: "cell-sub" }, `${d.chunk_count} section${d.chunk_count === 1 ? "" : "s"} of knowledge`) : null,
            d.status === "failed" ? h("div", { class: "cell-error" }, d.status_message) : null))),
      h("td", { "data-label": "Type" }, h("span", { class: "chip" }, d.file_type)),
      compact ? null : h("td", { "data-label": "Size", class: "cell-muted" }, formatBytes(d.size_bytes)),
      compact ? null : h("td", { "data-label": "Uploaded", class: "cell-muted" }, formatDay(d.uploaded_at)),
      statusCell,
      h("td", { class: "col-actions" },
        h("div", { style: "display:inline-flex;gap:6px;align-items:center" },
          d.status === "failed" ? h("button", { type: "button", class: "btn btn-sm", onclick: (e) => reprocess(d, e.currentTarget) }, "Retry") : null,
          actionMenu(`Actions for ${d.filename}`, [
            { label: "View Details", icon: "info", onClick: () => showDetails(d) },
            { label: "Reprocess", icon: "refresh", disabled: processing, onClick: () => reprocess(d) },
            { label: "Delete", icon: "trash", danger: true, onClick: () => remove(d) },
          ]),
        )),
    );
  }

  async function showDetails(d) {
    const body = h("div", {}, loadingBlock());
    const m = modal({ title: d.filename, wide: true, body, actions: [h("button", { type: "button", class: "btn", onclick: () => m.close() }, "Close")] });
    try {
      const det = await api.get(`/api/documents/${d.id}`);
      mount(body,
        h("dl", { class: "info-list" },
          h("dt", {}, "Status"), h("dd", {}, statusBadge(det.status)),
          h("dt", {}, "Type"), h("dd", {}, det.file_type),
          h("dt", {}, "Size"), h("dd", {}, formatBytes(det.size_bytes)),
          h("dt", {}, "Uploaded"), h("dd", {}, formatDateTime(det.uploaded_at)),
          h("dt", {}, "Processed"), h("dd", {}, formatDateTime(det.processed_at)),
          h("dt", {}, "Knowledge sections"), h("dd", {}, String(det.chunk_count)),
          det.status === "failed" ? [h("dt", {}, "Problem"), h("dd", { class: "cell-error" }, det.status_message)] : null,
        ),
        det.preview.length ? h("div", { style: "margin-top:18px" },
          h("div", { class: "section-title" }, "What the assistant learned (first sections)"),
          h("div", { class: "stack" }, det.preview.map((p) => h("div", { class: "excerpt" },
            p.page ? h("span", { class: "chip", style: "margin-right:6px" }, `Page ${p.page}`) : null, p.text)))) : null,
      );
    } catch (e) {
      mount(body, h("p", { class: "form-error" }, e.message));
    }
  }

  async function reprocess(d, button) {
    const run = async () => {
      try {
        await api.post(`/api/documents/${d.id}/reprocess`);
        notify.info("Processing started", d.filename);
      } catch (e) {
        notify.error("Could not reprocess", e.message);
      }
      await reload();
    };
    if (button) await busy(button, "Starting…", run); else await run();
  }

  async function remove(d) {
    const ok = await confirmDialog({
      title: "Delete this document?",
      message: h("div", {},
        h("p", {}, "This will remove the document from the knowledge base."),
        h("p", { style: "margin-top:8px" }, h("strong", {}, d.filename)),
        h("p", { style: "margin-top:8px;color:var(--text-3)" }, "The assistant will no longer use its information in answers or replies.")),
      confirmLabel: "Delete", busyLabel: "Deleting…", danger: true,
      action: () => api.del(`/api/documents/${d.id}`),
    });
    if (ok) {
      notify.success("Document deleted", d.filename);
      docs = docs.filter((x) => x.id !== d.id);
      draw();
      changed("documents");
      reload();
    }
  }

  async function uploadFiles(files) {
    files = Array.from(files || []);
    if (!files.length) return;
    uploadsEl.hidden = false;
    const allowed = new Set(k.extensions);
    const items = files.map((f) => {
      const bar = h("div");
      const statusText = h("div", { class: "cell-sub" }, "Waiting…");
      const item = h("li", { class: "upload-item" }, icon("file", 18),
        h("div", { class: "grow" }, h("div", { class: "cell-title truncate" }, f.name), statusText, h("div", { class: "progress" }, bar)));
      uploadsEl.prepend(item);
      return { f, el: item, bar, statusText };
    });
    for (const it of items) {
      const ext = "." + (it.f.name.split(".").pop() || "").toLowerCase();
      if (!allowed.has(ext)) { failItem(it, `Unsupported file type. Use ${k.types.join(", ")}.`); continue; }
      if (it.f.size > k.max_upload_mb * 1024 * 1024) { failItem(it, `Larger than ${k.max_upload_mb} MB.`); continue; }
      it.statusText.textContent = "Uploading…";
      const fd = new FormData();
      fd.append("files", it.f, it.f.name);
      try {
        const res = await api.upload("/api/documents/upload", fd, (p) => { it.bar.style.width = `${Math.round(p * 100)}%`; });
        if (res.failed && res.failed.length) { failItem(it, res.failed[0].error); continue; }
        it.bar.style.width = "100%";
        it.statusText.textContent = "Document uploaded — processing continues below";
        setTimeout(() => { it.el.remove(); if (!uploadsEl.children.length) uploadsEl.hidden = true; }, 2500);
      } catch (e) {
        failItem(it, e.message);
      }
    }
    changed("documents");
    await reload();
  }

  function failItem(it, message) {
    mount(it.statusText, h("span", { class: "cell-error" }, `Upload failed. ${message}`));
    it.bar.style.width = "0";
    it.el.append(
      h("button", { type: "button", class: "btn btn-sm", onclick: () => { it.el.remove(); uploadFiles([it.f]); } }, "Retry"),
      h("button", { type: "button", class: "icon-btn", "aria-label": "Dismiss", onclick: () => { it.el.remove(); if (!uploadsEl.children.length) uploadsEl.hidden = true; } }, icon("close", 16)),
    );
  }

  fileInput.addEventListener("change", () => { uploadFiles(fileInput.files); fileInput.value = ""; });
  ["dragenter", "dragover"].forEach((ev) => dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.add("dragover"); }));
  ["dragleave", "drop"].forEach((ev) => dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.remove("dragover"); }));
  dropzone.addEventListener("drop", (e) => uploadFiles(e.dataTransfer.files));
  ctx.onLeave(on("documents", () => { if (!pollStop) reload(); }));

  return { el, uploadButton, reload };
}
