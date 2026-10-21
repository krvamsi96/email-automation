// Shared UI building blocks. All text is inserted as text nodes (never innerHTML),
// so email content and file names can never inject markup.
import { icon } from "./icons.js";

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === "value") el.value = value;
    else if (key === "checked" || key === "disabled" || key === "readOnly" || key === "hidden" || key === "multiple" || key === "selected") el[key] = !!value;
    else el.setAttribute(key, value === true ? "" : value);
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

export function mount(el, ...children) {
  el.replaceChildren();
  append(el, children);
  return el;
}

// ---------- status system (one vocabulary for every screen) ----------
export const STATUS = {
  // documents
  ready: ["Ready", "success"],
  processing: ["Processing", "info"],
  failed: ["Failed", "danger"],
  // emails
  new: ["New", "neutral"],
  processed: ["Processed", "success"],
  needs_attention: ["Needs Human Review", "warning"],
  flagged: ["Security Review", "danger"],
  skipped: ["Skipped", "neutral"],
  // drafts
  pending_review: ["Pending Review", "warning"],
  edited: ["Edited", "info"],
  approved: ["Approved", "success"],
  sent: ["Sent", "success"],
  rejected: ["Rejected", "neutral"],
  // knowledge tests
  answered: ["Answered", "success"],
  no_knowledge: ["No Knowledge Found", "warning"],
  ai_unavailable: ["AI Unavailable", "danger"],
  // accounts
  connected: ["Connected", "success"],
  disconnected: ["Disconnected", "danger"],
  error: ["Connection Error", "danger"],
};

export function statusBadge(status) {
  const [label, tone] = STATUS[status] || [status || "Unknown", "neutral"];
  return h("span", { class: `badge badge-${tone}${status === "processing" ? " badge-processing" : ""}` }, label);
}

export const CATEGORY = {
  faq_question: "Question",
  support_request: "Support Request",
  general_inquiry: "General Inquiry",
  human_assistance: "Needs Specialist",
  unrelated: "Promotional",
  security: "Security",
};
export function categoryLabel(c) { return c ? (CATEGORY[c] || c.replace(/_/g, " ")) : "—"; }

// ---------- formatting ----------
export function parseDate(v) {
  if (!v) return null;
  const d = new Date(v);
  return isNaN(d) ? null : d;
}
export function formatDateTime(v) {
  const d = parseDate(v);
  if (!d) return "—";
  return d.toLocaleString(undefined, { month: "short", day: "numeric", year: d.getFullYear() !== new Date().getFullYear() ? "numeric" : undefined, hour: "numeric", minute: "2-digit" });
}
export function formatDay(v) {
  const d = parseDate(v);
  if (!d) return "—";
  const today = new Date();
  const yest = new Date(); yest.setDate(today.getDate() - 1);
  if (d.toDateString() === today.toDateString()) return "Today";
  if (d.toDateString() === yest.toDateString()) return "Yesterday";
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: d.getFullYear() !== today.getFullYear() ? "numeric" : undefined });
}
export function timeAgo(v) {
  const d = parseDate(v);
  if (!d) return "—";
  const s = Math.round((Date.now() - d.getTime()) / 1000);
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  if (s < 7 * 86400) return `${Math.round(s / 86400)} d ago`;
  return formatDay(v);
}
export function formatBytes(n) {
  if (!n) return "0 KB";
  if (n < 1024 * 1024) return `${Math.max(1, Math.round(n / 1024))} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}
export function senderName(sender) {
  if (!sender) return "Unknown sender";
  const m = sender.match(/^\s*"?([^"<]*)"?\s*<([^>]+)>/);
  if (m) return m[1].trim() || m[2];
  return sender.trim();
}
export function senderAddress(sender) {
  if (!sender) return "";
  const m = sender.match(/<([^>]+)>/);
  return m ? m[1] : sender.trim();
}
export function initials(text) {
  const words = String(text || "?").replace(/[^\p{L}\p{N} ]/gu, " ").trim().split(/\s+/).filter(Boolean);
  return (words.length > 1 ? words[0][0] + words[1][0] : (words[0] || "?").slice(0, 2)).toUpperCase();
}

// ---------- states ----------
export function loadingBlock(text = "Loading…") {
  return h("div", { class: "loading-block", role: "status" }, h("div", { class: "spinner spinner-lg" }), h("div", {}, text));
}
export function emptyState({ iconName = "inbox", title, text, action }) {
  return h("div", { class: "empty" },
    h("div", { class: "empty-icon" }, icon(iconName, 26)),
    h("h3", {}, title),
    text ? h("p", {}, text) : null,
    action || null,
  );
}
export function errorState(message, onRetry) {
  return h("div", { class: "empty" },
    h("div", { class: "empty-icon", style: "background:var(--danger-bg);color:var(--danger)" }, icon("error", 26)),
    h("h3", {}, "Something went wrong"),
    h("p", {}, message || "The information could not be loaded."),
    onRetry ? h("button", { class: "btn", type: "button", onclick: onRetry }, icon("refresh", 16), "Retry") : null,
  );
}
export function alertBox(tone, title, body, actions) {
  const iconName = { warning: "warning", danger: "error", info: "info", success: "shieldCheck" }[tone] || "info";
  return h("div", { class: `alert alert-${tone}`, role: tone === "danger" ? "alert" : null },
    icon(iconName, 18),
    h("div", { class: "alert-body" },
      title ? h("p", { class: "alert-title" }, title) : null,
      body ? (body instanceof Node ? body : h("p", {}, body)) : null,
      actions && actions.length ? h("div", { class: "alert-actions" }, actions) : null,
    ),
  );
}

// ---------- buttons with loading state ----------
// Runs an async action while the button shows a spinner and cannot be clicked again.
export async function busy(button, label, fn) {
  if (!button || button.dataset.busy === "1") return undefined;
  const original = Array.from(button.childNodes);
  const width = button.offsetWidth;
  button.dataset.busy = "1";
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  if (width) button.style.minWidth = `${width}px`;
  mount(button, h("span", { class: "spinner" }), label);
  try {
    return await fn();
  } finally {
    if (button.isConnected) {
      button.replaceChildren(...original);
      button.disabled = false;
      button.removeAttribute("aria-busy");
      button.style.minWidth = "";
    }
    delete button.dataset.busy;
  }
}

// ---------- toasts ----------
export function toast(kind, title, message = "", { timeout = 5000 } = {}) {
  const root = document.getElementById("toasts");
  const close = h("button", { type: "button", "aria-label": "Dismiss" }, "×");
  const el = h("div", { class: `toast toast-${kind}` },
    h("div", { class: "grow" }, h("div", { class: "toast-title" }, title), message ? h("div", { class: "toast-msg" }, message) : null),
    close,
  );
  const remove = () => el.remove();
  close.addEventListener("click", remove);
  root.appendChild(el);
  if (timeout) setTimeout(remove, timeout);
}
export const notify = {
  success: (t, m) => toast("success", t, m),
  error: (t, m) => toast("error", t, m, { timeout: 8000 }),
  warning: (t, m) => toast("warning", t, m, { timeout: 7000 }),
  info: (t, m) => toast("info", t, m),
};

// ---------- modal dialogs ----------
let openModals = 0;
export function modal({ title, body, actions = [], wide = false, onClose }) {
  const root = document.getElementById("modalRoot");
  const previousFocus = document.activeElement;
  const titleId = `m-${Math.random().toString(36).slice(2)}`;
  const box = h("div", { class: `modal${wide ? " modal-wide" : ""}`, role: "dialog", "aria-modal": "true", "aria-labelledby": titleId },
    h("div", { class: "modal-head" }, h("h2", { id: titleId }, title)),
    h("div", { class: "modal-body" }, body),
    actions.length ? h("div", { class: "modal-foot" }, actions) : null,
  );
  const backdrop = h("div", { class: "modal-backdrop" }, box);
  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    backdrop.remove();
    document.removeEventListener("keydown", onKey, true);
    openModals -= 1;
    if (previousFocus && previousFocus.focus) previousFocus.focus();
    if (onClose) onClose();
  };
  const onKey = (e) => {
    if (e.key === "Escape" && !box.querySelector("[aria-busy=true]")) { e.stopPropagation(); close(); }
    if (e.key === "Tab") {
      const f = box.querySelectorAll("button:not([disabled]), input:not([disabled]), textarea, select, a[href]");
      if (!f.length) return;
      const first = f[0], last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  };
  backdrop.addEventListener("mousedown", (e) => { if (e.target === backdrop && !box.querySelector("[aria-busy=true]")) close(); });
  document.addEventListener("keydown", onKey, true);
  root.appendChild(backdrop);
  openModals += 1;
  const focusTarget = box.querySelector("[autofocus], input, textarea, .modal-foot .btn:last-child");
  if (focusTarget) focusTarget.focus();
  return { close, box };
}

// Confirmation dialog. `action` runs while the confirm button shows a loading state;
// the dialog closes only when it succeeds, and shows the error otherwise.
export function confirmDialog({ title, message, confirmLabel = "Confirm", busyLabel = "Working…", danger = false, action, extra }) {
  return new Promise((resolve) => {
    const errorEl = h("p", { class: "form-error", hidden: true, role: "alert" });
    const cancel = h("button", { type: "button", class: "btn" }, "Cancel");
    const ok = h("button", { type: "button", class: `btn ${danger ? "btn-danger" : "btn-primary"}` }, confirmLabel);
    let result = false;
    const m = modal({
      title,
      body: h("div", { class: "stack" }, typeof message === "string" ? h("p", {}, message) : message, extra || null, errorEl),
      actions: [cancel, ok],
      onClose: () => resolve(result),
    });
    cancel.addEventListener("click", () => m.close());
    ok.addEventListener("click", async () => {
      errorEl.hidden = true;
      if (!action) { result = true; m.close(); return; }
      try {
        cancel.disabled = true;
        result = await busy(ok, busyLabel, action);
        if (result === undefined) result = true;
        m.close();
      } catch (err) {
        errorEl.textContent = err.message || "Something went wrong. Please try again.";
        errorEl.hidden = false;
        cancel.disabled = false;
      }
    });
  });
}

// ---------- dropdown menu ----------
let openMenu = null;
document.addEventListener("click", (e) => {
  if (openMenu && !openMenu.wrap.contains(e.target)) closeMenu();
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && openMenu) closeMenu(); });
function closeMenu() {
  if (!openMenu) return;
  openMenu.menu.remove();
  openMenu.trigger.setAttribute("aria-expanded", "false");
  openMenu = null;
}
export function actionMenu(label, items) {
  const trigger = h("button", { type: "button", class: "icon-btn", "aria-label": label, "aria-haspopup": "menu", "aria-expanded": "false" }, icon("more", 18));
  const wrap = h("div", { class: "menu-wrap" }, trigger);
  trigger.addEventListener("click", (e) => {
    e.stopPropagation();
    if (openMenu && openMenu.wrap === wrap) return closeMenu();
    closeMenu();
    const menu = h("div", { class: "menu", role: "menu" },
      items.filter(Boolean).map((it) => h("button", {
        type: "button", role: "menuitem", class: it.danger ? "danger" : "", disabled: it.disabled,
        onclick: (ev) => { ev.stopPropagation(); closeMenu(); it.onClick(); },
      }, it.icon ? icon(it.icon, 16) : null, it.label)),
    );
    wrap.appendChild(menu);
    trigger.setAttribute("aria-expanded", "true");
    openMenu = { wrap, menu, trigger };
    const first = menu.querySelector("button:not([disabled])");
    if (first) first.focus();
  });
  return wrap;
}

export function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}
