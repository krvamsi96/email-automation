import { api } from "../api.js";
import { icon } from "../icons.js";
import { appState, on } from "../store.js";
import { h, mount, loadingBlock, errorState, timeAgo } from "../ui.js";
import { pageHead, checkEmailsControl } from "./common.js";

const ACTIVITY_TONE = {
  draft_created: ["sparkle", "info"], draft_approved: ["check", "success"], draft_sent: ["send", "success"],
  draft_rejected: ["close", "neutral"], draft_edited: ["edit", "info"], draft_failed: ["error", "danger"],
  emails_received: ["mail", "info"], email_flagged: ["warning", "danger"], needs_attention: ["warning", "warning"],
  processing_failed: ["warning", "warning"], document_uploaded: ["upload", "info"], document_ready: ["book", "success"],
  document_failed: ["error", "danger"], document_deleted: ["trash", "neutral"], account_connected: ["account", "success"],
  account_reconnected: ["account", "success"], account_disconnected: ["account", "neutral"], account_tested: ["check", "success"],
};

function stat({ label, value, sub, iconName, href, highlight }) {
  const tag = href ? "a" : "div";
  return h(tag, { class: `stat${highlight ? " highlight" : ""}`, href: href ? `#${href}` : null },
    h("div", { class: "stat-label" }, h("span", { class: "stat-icon" }, icon(iconName, 16)), label),
    h("div", { class: "stat-value" }, String(value)),
    sub ? h("div", { class: "stat-sub" }, sub) : null,
  );
}

export async function render(ctx) {
  const root = ctx.el;
  mount(root, loadingBlock());

  let data;
  async function load() {
    data = await api.get("/api/dashboard/stats");
  }
  try {
    await load();
  } catch (e) {
    if (ctx.alive()) mount(root, errorState(e.message, () => render(ctx)));
    return;
  }
  if (!ctx.alive()) return;

  const info = appState.info;
  const hasAccounts = data.accounts.total > 0;
  if (!hasAccounts) {
    mount(root, connectGate(data));
    ctx.onLeave(on("accounts", () => render(ctx)));
    return;
  }
  const check = checkEmailsControl(ctx, { hasAccounts, onFinished: refresh });
  const body = h("div");
  mount(root,
    pageHead("Email Assistant", `Incoming emails and AI reply drafts for ${info.branding.company_name}.`, [
      h("span", { class: "badge badge-success" }, `Gmail Connected ✓ (${data.accounts.connected})`), check.button]),
    check.banner,
    workflowStrip(),
    body,
  );
  check.resume(data.job);
  draw();

  async function refresh() {
    try { await load(); } catch { return; }
    if (ctx.alive()) draw();
  }
  ctx.onLeave(on("*", (topic) => { if (topic !== "app") refresh(); }));
  ctx.interval(refresh, 30000);

  function draw() {
    const e = data.emails, d = data.drafts, docs = data.documents, acc = data.accounts;
    const setupDone = hasAccounts && docs.ready > 0;
    mount(body,
      h("section", { class: "stats", "aria-label": "Key figures" },
        stat({ label: "Total Emails", value: e.total, sub: `${e.processed} processed`, iconName: "mail", href: "/emails" }),
        stat({ label: "Pending Review", value: d.pending_review, sub: "AI drafts waiting for approval", iconName: "approval", href: "/approvals", highlight: d.pending_review > 0 }),
        stat({ label: "Drafts Created", value: d.total, sub: `${d.approved} approved · ${d.sent} sent`, iconName: "draft", href: "/drafts" }),
        stat({ label: "Needs Attention", value: e.needs_attention + e.flagged, sub: `${e.flagged} security review`, iconName: "warning", href: "/emails?status=needs_attention" }),
        stat({ label: "Documents", value: docs.ready, sub: docs.processing ? `${docs.processing} processing` : `${docs.total} in knowledge base`, iconName: "book", href: "/knowledge" }),
        stat({ label: "Email Accounts", value: acc.connected, sub: acc.last_checked_at ? `Last checked ${timeAgo(acc.last_checked_at)}` : "Not checked yet", iconName: "account", href: "/accounts" }),
      ),
      h("div", { class: "split" },
        h("section", { class: "card" },
          h("div", { class: "card-head" }, h("h2", {}, "Recent Activity")),
          data.activity.length
            ? h("ul", { class: "activity" }, data.activity.map(activityItem))
            : h("div", { class: "empty" }, h("p", {}, "Activity will appear here as emails are processed and knowledge is added.")),
        ),
        h("div", { class: "stack" },
          setupDone ? null : setupCard(hasAccounts, docs),
          howItWorks(),
        ),
      ),
    );
  }
}

const EMAIL_FLOW = ["New Email", "Understand Email", "Identify Question", "Search Knowledge", "Generate AI Response", "Create Gmail Draft", "Human Review", "Human Sends"];

function workflowStrip() {
  return h("ol", { class: "flow flow-compact mb", "aria-label": "Email Assistant workflow" },
    EMAIL_FLOW.map((t, i) => h("li", { class: `flow-step ${i >= 6 ? "flow-human" : "flow-done"}` },
      h("span", { class: "flow-dot" }, icon(i >= 6 ? "account" : i === 5 ? "draft" : "sparkle", 14)),
      h("span", { class: "flow-title" }, t),
      i < EMAIL_FLOW.length - 1 ? h("span", { class: "flow-line", "aria-hidden": "true" }) : null)));
}

function connectGate(data) {
  const docs = data.documents;
  return h("div", {},
    pageHead("Email Assistant", "Let the AI prepare reply drafts for incoming emails."),
    h("section", { class: "card gate" },
      h("div", { class: "card-body", style: "display:grid;justify-items:center;gap:12px;text-align:center;padding:40px 24px" },
        h("div", { class: "mode-icon" }, icon("mail", 26)),
        h("h2", { style: "font-size:18px" }, "Connect Gmail to use Email Assistant"),
        h("p", { style: "color:var(--text-2);max-width:520px" },
          "Connect your Gmail account to automatically process incoming emails and prepare response drafts. Nothing is ever sent without a person clicking Send."),
        h("a", { class: "btn btn-primary btn-lg", href: "#/accounts?connect=1" }, "Connect Gmail"),
        h("p", { class: "hint", style: "color:var(--text-3)" },
          docs.ready ? `Your Knowledge Base already has ${docs.ready} ready document${docs.ready === 1 ? "" : "s"}. ` : "",
          "Want to try the AI first? ", h("a", { href: "#/knowledge-test" }, "Use the Knowledge Base Test"), " — no Gmail needed."),
      )),
    h("div", { style: "margin-top:20px" }, workflowStrip()),
  );
}

function activityItem(a) {
  const [iconName, tone] = ACTIVITY_TONE[a.kind] || ["info", "neutral"];
  const href = a.ref_type === "draft" ? `#/drafts/${a.ref_id}` : a.ref_type === "email" ? `#/emails/${a.ref_id}` :
    a.ref_type === "document" ? "#/knowledge" : a.ref_type === "account" ? "#/accounts" : null;
  return h("li", {},
    h("div", { class: `a-icon a-${tone}` }, icon(iconName, 16)),
    h("div", { class: "a-text" },
      href ? h("a", { class: "a-msg", href, style: "color:inherit" }, a.message) : h("div", { class: "a-msg" }, a.message),
      h("div", { class: "a-time" }, timeAgo(a.created_at)),
    ),
  );
}

function setupCard(hasAccounts, docs) {
  const item = (done, title, text, href, cta) => h("li", {},
    h("span", { class: `check${done ? " done" : ""}` }, done ? icon("check", 14) : null),
    h("div", { class: "grow" }, h("div", { style: "font-weight:600" }, title), h("div", { class: "muted" }, text)),
    done ? null : h("a", { class: "btn btn-sm", href: `#${href}` }, cta),
  );
  return h("section", { class: "card" },
    h("div", { class: "card-head" }, h("h2", {}, "Get started")),
    h("ul", { class: "checklist" },
      item(hasAccounts, "Connect your email", "Link the Gmail inbox the assistant should answer.", "/accounts", "Connect"),
      item(docs.ready > 0, "Add company knowledge", "Upload FAQs, policies or guides to ground replies.", "/knowledge", "Upload"),
    ),
  );
}

function howItWorks() {
  return h("section", { class: "card" },
    h("div", { class: "card-head" }, h("h2", {}, "How it works")),
    h("div", { class: "card-body" },
      h("ol", { class: "help-steps" },
        h("li", {}, "New emails arrive in your connected inbox."),
        h("li", {}, "The assistant reads each email and finds answers in your Knowledge Base."),
        h("li", {}, "A suggested reply is saved as a Gmail draft."),
        h("li", {}, "You review, edit and approve it, then send it from Gmail."),
      ),
    ),
  );
}
