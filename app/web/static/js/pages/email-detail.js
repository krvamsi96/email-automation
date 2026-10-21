import { api } from "../api.js";
import { icon } from "../icons.js";
import { changed } from "../store.js";
import { h, mount, loadingBlock, errorState, statusBadge, categoryLabel, formatDateTime, alertBox, confirmDialog, notify } from "../ui.js";
import { sourcesChips } from "./common.js";

const THREAT_LABEL = { prompt_injection: "Attempt to manipulate the assistant", phishing: "Possible phishing", spam: "Spam" };

export async function render(ctx) {
  const root = ctx.el;
  mount(root, loadingBlock("Loading email…"));
  let email;
  try {
    email = await api.get(`/api/emails/${encodeURIComponent(ctx.params.id)}`);
  } catch (e) {
    if (!ctx.alive()) return;
    mount(root, backLink(), errorState(e.status === 404 ? "This email could not be found." : e.message, e.status === 404 ? null : () => render(ctx)));
    return;
  }
  if (!ctx.alive()) return;

  const draft = email.drafts.find((d) => d.status !== "rejected") || email.drafts[0];
  const reprocessBtn = h("button", { type: "button", class: "btn" }, icon("refresh", 16), "Process again");
  const canReprocess = !email.processing && !(draft && ["approved", "sent"].includes(draft.status));
  reprocessBtn.disabled = !canReprocess;
  if (!canReprocess) reprocessBtn.title = email.processing ? "Processing in progress" : "A reply has already been approved or sent";
  reprocessBtn.addEventListener("click", async () => {
    const replacing = draft && ["pending_review", "edited", "failed"].includes(draft.status);
    const done = await confirmDialog({
      title: "Process this email again?",
      message: replacing
        ? "The assistant will generate a new response. The current draft will be removed from Gmail and replaced."
        : "The assistant will read this email again and generate a new response.",
      confirmLabel: "Generate response",
      busyLabel: "Generating response…",
      action: () => api.post(`/api/emails/${email.id}/process`),
    });
    if (done) {
      notify.success("Email processed", "The latest result is shown below.");
      changed("emails", "drafts");
      if (ctx.alive()) render(ctx);
    }
  });

  mount(root,
    backLink(),
    h("div", { class: "page-head" },
      h("div", {}, h("h1", {}, email.subject || "(No subject)"),
        h("p", {}, "Received ", formatDateTime(email.received_at))),
      h("div", { class: "page-actions" }, statusBadge(email.status), reprocessBtn),
    ),
    notices(email),
    h("div", { class: "split" },
      h("div", { class: "stack" },
        h("section", { class: "card" },
          h("div", { class: "card-head" }, h("h2", {}, "Incoming Email")),
          h("div", { class: "card-body" },
            h("dl", { class: "meta-grid" },
              h("dt", {}, "From"), h("dd", {}, email.sender),
              h("dt", {}, "To"), h("dd", {}, email.recipient || email.account_email),
              h("dt", {}, "Subject"), h("dd", {}, email.subject),
              h("dt", {}, "Received"), h("dd", {}, formatDateTime(email.received_at)),
            ),
            h("div", { class: "divider" }),
            h("div", { class: "email-body scroll" }, email.body || "(This email has no text content.)"),
          ),
        ),
        understandingCards(email),
        h("div", { class: "flow-arrow", "aria-hidden": "true" }, icon("arrowDown", 22)),
        aiResponseCard(email, draft),
      ),
      h("div", { class: "stack" },
        h("section", { class: "card" },
          h("div", { class: "card-head" }, h("h2", {}, "AI Processing")),
          h("div", { class: "card-body" },
            h("dl", { class: "meta-grid" },
              h("dt", {}, "Status"), h("dd", {}, statusBadge(email.status)),
              h("dt", {}, "Category"), h("dd", {}, categoryLabel(email.category)),
              h("dt", {}, "Processed"), h("dd", {}, formatDateTime(email.processed_at)),
              h("dt", {}, "Account"), h("dd", {}, email.account_email || "—"),
            ),
            email.knowledge_sources.length ? h("div", { style: "margin-top:16px" },
              h("div", { class: "section-title" }, "Sources"), sourcesChips(email.knowledge_sources)) : null,
          ),
        ),
        threadCard(email),
      ),
    ),
  );
}

function backLink() {
  return h("a", { class: "back-link", href: "#/emails" }, icon("back", 16), "Back to emails");
}

function notices(email) {
  const items = [];
  if (email.status === "flagged") {
    items.push(alertBox("danger", "Security review required",
      `${THREAT_LABEL[email.security_flag] || "Suspicious content"}. ${email.status_message} Review this email carefully before replying.`));
  } else if (email.status === "needs_attention" || email.status === "failed") {
    items.push(alertBox("warning", email.status === "failed" ? "Processing failed" : "Needs human review", email.status_message));
  } else if (email.status === "skipped") {
    items.push(alertBox("info", "No reply needed", email.status_message));
  }
  return items.length ? h("div", { class: "mb" }, items) : null;
}

// "What did the AI understand, and what knowledge did it use?" — shown between the email and the reply.
function understandingCards(email) {
  if (!email.processed_at || ["flagged", "skipped"].includes(email.status)) return null;
  const arrow = () => h("div", { class: "flow-arrow", "aria-hidden": "true" }, icon("arrowDown", 22));
  const knowledge = email.evidence && email.evidence.length
    ? h("div", { class: "stack" }, groupEvidence(email.evidence).map(([doc, items]) => h("div", { class: "source-card" },
        h("div", { class: "source-name" }, icon("file", 16), doc),
        items.map((x) => h("div", { class: "excerpt" }, x.page ? h("span", { class: "chip", style: "margin-right:6px" }, `Page ${x.page}`) : null, x.text)))))
    : h("div", {},
        h("dl", { class: "info-list" },
          h("dt", {}, "Result"), h("dd", {}, h("strong", {}, "No reliable information found.")),
          h("dt", {}, "Recommended Action"), h("dd", {}, h("span", { class: "badge badge-warning" }, "Human Review"))));
  return [
    arrow(),
    h("section", { class: "card" },
      h("div", { class: "card-head" }, h("h2", {}, "Detected Request")),
      h("div", { class: "card-body io-question" }, email.question || "—")),
    arrow(),
    h("section", { class: "card" },
      h("div", { class: "card-head" }, h("h2", {}, "Knowledge Used")),
      h("div", { class: "card-body" }, knowledge)),
  ];
}

function groupEvidence(evidence) {
  const map = new Map();
  for (const e of evidence) {
    if (!map.has(e.document)) map.set(e.document, []);
    map.get(e.document).push(e);
  }
  return [...map.entries()];
}

function aiResponseCard(email, draft) {
  const head = h("div", { class: "card-head" },
    h("h2", { class: "ai-label" }, icon("sparkle", 18), "Suggested Email Response"),
    draft ? statusBadge(draft.status) : null);
  if (!draft) {
    const text = email.status === "flagged" ? "No response was generated because this email needs a security review."
      : email.status === "skipped" ? "No response was generated because this email does not need a reply."
      : email.status === "processing" || email.processing ? "Generating response…"
      : "No AI response has been generated for this email.";
    return h("section", { class: "card" }, head, h("div", { class: "card-body", style: "color:var(--text-3)" }, text));
  }
  const reviewable = ["pending_review", "edited"].includes(draft.status);
  return h("section", { class: "card review-card" }, head,
    h("div", { class: "card-body stack" },
      reviewable ? alertBox("warning", "Pending human review", "This reply is saved as a Gmail draft and will not be sent until a person approves and sends it.") : null,
      draft.needs_attention && reviewable ? alertBox("info", "Check carefully", draft.attention_reason) : null,
      draft.status === "failed" ? alertBox("danger", "Draft not saved to Gmail", draft.status_message) : null,
      h("div", { class: "email-body" }, draft.body),
    ),
    h("div", { class: "card-foot", style: "justify-content:space-between" },
      h("div", { class: "draft-result" },
        draft.status === "failed"
          ? h("span", { style: "color:var(--danger)" }, "Gmail draft not created")
          : [h("span", { style: "color:var(--success);font-weight:650" }, "Gmail Draft Created ✓"),
             h("span", { style: "color:var(--text-3)" }, " · Draft Status: "), statusBadge(draft.status)]),
      h("div", { style: "display:flex;gap:8px" },
        reviewable ? h("a", { class: "btn", href: `#/drafts/${draft.id}` }, icon("edit", 14), "Edit Response") : null,
        h("a", { class: `btn ${reviewable || draft.status === "failed" ? "btn-primary" : ""}`, href: `#/drafts/${draft.id}` },
          reviewable ? "Review & approve" : "View draft")),
    ),
  );
}

function threadCard(email) {
  const body = email.thread_error
    ? h("div", { class: "card-body" }, alertBox("warning", "Conversation unavailable", email.thread_error))
    : email.thread.length
      ? h("div", { class: "card-body thread" }, email.thread.map((m) => h("div", { class: `thread-msg${m.is_sent || m.is_draft ? " mine" : ""}` },
        h("div", { class: "thread-head" },
          h("strong", {}, m.is_draft ? "Draft reply" : m.sender),
          h("span", {}, formatDateTime(m.date))),
        h("div", { class: "email-body", style: "max-height:220px;overflow:auto" }, m.body || "(no text)"),
      )))
      : h("div", { class: "card-body", style: "color:var(--text-3)" }, "No other messages in this conversation.");
  return h("section", { class: "card" },
    h("div", { class: "card-head" }, h("h2", {}, "Conversation"), email.thread.length ? h("span", { class: "chip" }, `${email.thread.length} message(s)`) : null),
    body,
  );
}
