import { api } from "../api.js";
import { icon } from "../icons.js";
import { changed } from "../store.js";
import { h, mount, loadingBlock, errorState, statusBadge, formatDateTime, alertBox, confirmDialog, busy, notify } from "../ui.js";
import { setLeaveGuard, navigate } from "../router.js";

const REVIEWABLE = ["pending_review", "edited"];

export async function render(ctx) {
  const root = ctx.el;
  const fromApprovals = ctx.path.startsWith("/approvals");
  const backHref = fromApprovals ? "#/approvals" : "#/drafts";
  const back = h("a", { class: "back-link", href: backHref }, icon("back", 16), fromApprovals ? "Back to approvals" : "Back to drafts");
  mount(root, back, loadingBlock("Loading draft…"));

  let draft;
  try {
    draft = await api.get(`/api/drafts/${encodeURIComponent(ctx.params.id)}`);
  } catch (e) {
    if (ctx.alive()) mount(root, back, errorState(e.status === 404 ? "This draft could not be found." : e.message, e.status === 404 ? null : () => render(ctx)));
    return;
  }
  if (!ctx.alive()) return;

  let editing = false;
  const textarea = h("textarea", { class: "textarea review-textarea", readOnly: true, "aria-label": "Suggested response", spellcheck: "true" });
  textarea.value = draft.body;
  const dirty = () => textarea.value.trim() !== draft.body.trim();

  const guard = async () => {
    if (!dirty()) return true;
    return confirmDialog({
      title: "Discard your changes?",
      message: "You have edits that have not been saved to the draft.",
      confirmLabel: "Discard changes",
      danger: true,
    });
  };
  guard.dirty = dirty;
  setLeaveGuard(guard);
  ctx.onLeave(() => setLeaveGuard(null));

  const editBtn = h("button", { type: "button", class: "btn" }, icon("edit", 16), "Edit");
  const saveBtn = h("button", { type: "button", class: "btn btn-primary", hidden: true }, "Save Changes");
  const cancelBtn = h("button", { type: "button", class: "btn btn-ghost", hidden: true }, "Cancel");
  const rejectBtn = h("button", { type: "button", class: "btn btn-danger-ghost" }, "Reject");
  const approveBtn = h("button", { type: "button", class: "btn btn-success" }, icon("check", 16), "Approve Draft");
  const unsavedNote = h("span", { class: "chip", hidden: true, style: "color:var(--warning)" }, "Unsaved changes");

  function setEditing(on) {
    editing = on;
    textarea.readOnly = !on;
    editBtn.hidden = on;
    saveBtn.hidden = !on;
    cancelBtn.hidden = !on;
    if (on) textarea.focus();
    updateDirty();
  }
  function updateDirty() {
    unsavedNote.hidden = !dirty();
    saveBtn.disabled = !dirty();
  }
  textarea.addEventListener("input", updateDirty);

  editBtn.addEventListener("click", () => setEditing(true));
  cancelBtn.addEventListener("click", () => { textarea.value = draft.body; setEditing(false); });

  saveBtn.addEventListener("click", () => busy(saveBtn, "Saving…", async () => {
    try {
      draft = await api.put(`/api/drafts/${draft.id}`, { body: textarea.value });
      textarea.value = draft.body;
      notify.success("Changes saved", "The Gmail draft has been updated.");
      changed("drafts");
      setEditing(false);
      redraw();
    } catch (e) {
      notify.error("Could not save changes", e.message);
      if (e.code === "draft_missing") reload();
    }
  }));

  rejectBtn.addEventListener("click", async () => {
    const reason = h("textarea", { class: "textarea", style: "min-height:80px", placeholder: "Optional: why is this reply not suitable?", "aria-label": "Reason" });
    const ok = await confirmDialog({
      title: "Reject this draft?",
      message: "The draft will be removed from Gmail. The original email stays in your inbox so you can reply personally.",
      extra: reason,
      confirmLabel: "Reject",
      busyLabel: "Rejecting…",
      danger: true,
      action: async () => { draft = await api.post(`/api/drafts/${draft.id}/reject`, { reason: reason.value }); },
    });
    if (ok) {
      textarea.value = draft.body;
      notify.info("Draft rejected", "It has been removed from Gmail.");
      changed("drafts", "emails");
      afterDecision();
    }
  });

  approveBtn.addEventListener("click", async () => {
    const withEdits = dirty();
    const ok = await confirmDialog({
      title: "Approve this draft?",
      message: withEdits
        ? "Your edits will be saved to the Gmail draft and it will be marked as approved."
        : "The draft will be marked as approved.",
      extra: alertBox("info", null, "The email is not sent automatically. Open Gmail and click Send when you are ready."),
      confirmLabel: "Approve Draft",
      busyLabel: "Approving…",
      action: async () => { draft = await api.post(`/api/drafts/${draft.id}/approve`, { body: textarea.value }); },
    });
    if (ok) {
      textarea.value = draft.body;
      notify.success("Draft approved", "Open Gmail to send it.");
      changed("drafts", "emails");
      afterDecision();
    }
  });

  function afterDecision() {
    setEditing(false);
    if (fromApprovals) {
      // Move to the next item in the queue, if any.
      api.get("/api/drafts?status=awaiting_review").then((res) => {
        if (!ctx.alive()) return;
        const next = res.items.slice().reverse().find((d) => d.id !== draft.id);
        if (next) redraw(next.id); else redraw();
      }).catch(() => redraw());
    } else {
      redraw();
    }
  }

  async function reload() {
    try { draft = await api.get(`/api/drafts/${draft.id}`); } catch { return; }
    if (!ctx.alive()) return;
    textarea.value = draft.body;
    setEditing(false);
    redraw();
  }

  const statusArea = h("div");
  const actionsArea = h("div");
  const email = draft.email || {};

  mount(root,
    back,
    h("div", { class: "page-head" },
      h("div", {}, h("h1", {}, "Review AI Response"), h("p", {}, draft.subject)),
      h("div", { class: "page-actions" }, h("span", { id: "statusSlot" })),
    ),
    statusArea,
    h("div", { class: "grid grid-2", style: "align-items:start" },
      h("section", { class: "card" },
        h("div", { class: "card-head" }, h("h2", {}, "Original Email"),
          email.id ? h("a", { href: `#/emails/${email.id}`, class: "btn btn-sm btn-ghost" }, "Open email") : null),
        h("div", { class: "card-body" },
          h("dl", { class: "meta-grid" },
            h("dt", {}, "From"), h("dd", {}, email.sender || draft.recipient),
            h("dt", {}, "Subject"), h("dd", {}, email.subject || ""),
            h("dt", {}, "Received"), h("dd", {}, formatDateTime(email.received_at)),
          ),
          h("div", { class: "divider" }),
          h("div", { class: "email-body scroll" }, email.body || "(The original email is no longer available.)"),
        ),
      ),
      h("section", { class: "card review-card" },
        h("div", { class: "card-head" }, h("h2", { class: "ai-label" }, icon("sparkle", 18), "Suggested Response"), unsavedNote),
        h("div", { class: "card-body" },
          h("dl", { class: "meta-grid", style: "margin-bottom:12px" },
            h("dt", {}, "To"), h("dd", {}, draft.recipient),
            h("dt", {}, "Subject"), h("dd", {}, draft.subject),
            h("dt", {}, "Created"), h("dd", {}, formatDateTime(draft.created_at)),
          ),
          textarea,
        ),
        actionsArea,
      ),
    ),
  );

  function redraw(nextId) {
    const slot = document.getElementById("statusSlot");
    if (slot) mount(slot, statusBadge(draft.status));
    const reviewable = REVIEWABLE.includes(draft.status);
    const notes = [];
    if (reviewable) {
      notes.push(alertBox("warning", "Pending Human Review",
        "This reply is saved as a draft in Gmail. Review it, edit if needed, then approve. It will not be sent automatically."));
      if (draft.needs_attention) notes.push(alertBox("info", "Check this reply carefully", draft.attention_reason));
    } else if (draft.status === "approved") {
      notes.push(alertBox("success", "Approved — ready to send",
        "Open Gmail, review the draft one last time and click Send. Status changes to Sent once it has been sent.",
        [
          draft.gmail_url ? h("a", { class: "btn btn-sm btn-primary", href: draft.gmail_url, target: "_blank", rel: "noopener noreferrer" }, icon("external", 14), "Open in Gmail") : null,
          syncButton(),
        ].filter(Boolean)));
    } else if (draft.status === "sent") {
      notes.push(alertBox("success", "Sent", "This reply was sent from Gmail."));
    } else if (draft.status === "rejected") {
      notes.push(alertBox("info", "Rejected", draft.status_message || "This draft was rejected and removed from Gmail."));
    } else if (draft.status === "failed") {
      const retry = h("button", { type: "button", class: "btn btn-sm btn-primary" }, "Retry");
      retry.addEventListener("click", () => busy(retry, "Creating draft…", async () => {
        try {
          draft = await api.post(`/api/drafts/${draft.id}/retry`);
          notify.success("Draft saved to Gmail", "It is now waiting for review.");
          changed("drafts", "emails");
          redraw();
        } catch (e) { notify.error("Could not create the draft", e.message); }
      }));
      notes.push(alertBox("danger", "The draft could not be saved to Gmail", draft.status_message, [retry]));
    }
    if (reviewable && !draft.account_connected) {
      notes.push(alertBox("danger", "Email account disconnected", "Reconnect the email account to edit or approve this draft.",
        [h("a", { class: "btn btn-sm", href: "#/accounts" }, "Reconnect")]));
    }
    if (nextId) {
      notes.push(alertBox("info", "Next draft waiting", "There are more drafts pending review.",
        [h("button", { type: "button", class: "btn btn-sm btn-primary", onclick: () => navigate(`/approvals/${nextId}`) }, "Review next")]));
    }
    mount(statusArea, notes.length ? h("div", { class: "stack mb" }, notes) : null);

    const canAct = reviewable && draft.account_connected;
    editBtn.disabled = !canAct;
    approveBtn.disabled = !canAct;
    rejectBtn.disabled = !(reviewable || draft.status === "failed");
    if (!reviewable && draft.status !== "failed") {
      mount(actionsArea);
      textarea.readOnly = true;
    } else {
      mount(actionsArea, h("div", { class: "card-foot" }, h("div", { class: "review-actions", style: "width:100%" },
        h("div", { class: "right" }, editBtn, cancelBtn, saveBtn),
        h("div", { class: "right" }, rejectBtn, reviewable ? approveBtn : null),
      )));
    }
    updateDirty();
  }

  function syncButton() {
    const b = h("button", { type: "button", class: "btn btn-sm" }, icon("refresh", 14), "Check if sent");
    b.addEventListener("click", () => busy(b, "Checking…", async () => {
      try {
        await api.post("/api/drafts/sync");
        await reload();
        if (draft.status === "sent") notify.success("Sent", "The reply was sent from Gmail.");
        else if (draft.status === "approved") notify.info("Not sent yet", "The draft is still waiting in Gmail.");
        changed("drafts");
      } catch (e) { notify.error("Could not check Gmail", e.message); }
    }));
    return b;
  }

  redraw();
}
