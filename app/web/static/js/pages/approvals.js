import { api } from "../api.js";
import { icon } from "../icons.js";
import { on } from "../store.js";
import { h, mount, loadingBlock, errorState, emptyState, statusBadge, timeAgo, senderName } from "../ui.js";
import { pageHead } from "./common.js";

export async function render(ctx) {
  const root = ctx.el;
  const listEl = h("div", {}, loadingBlock("Loading approvals…"));
  mount(root,
    pageHead("Approvals", "AI replies waiting for a person to review. Nothing is sent until you send it from Gmail."),
    listEl,
  );

  let seq = 0;
  async function load() {
    const mine = ++seq;
    let data;
    try {
      data = await api.get("/api/drafts?status=awaiting_review");
    } catch (e) {
      if (ctx.alive() && mine === seq) mount(listEl, h("div", { class: "card" }, errorState(e.message, load)));
      return;
    }
    if (!ctx.alive() || mine !== seq) return;
    if (!data.items.length) {
      mount(listEl, h("div", { class: "card" }, emptyState({
        iconName: "approval", title: "No drafts waiting for review",
        text: "You're all caught up. New AI drafts will appear here for approval.",
        action: h("a", { class: "btn", href: "#/drafts" }, "View all drafts"),
      })));
      return;
    }
    // Oldest first: the reply that has waited longest is reviewed first.
    const items = data.items.slice().reverse();
    mount(listEl,
      h("div", { class: "alert alert-warning mb" }, icon("warning", 18),
        h("div", { class: "alert-body" }, h("p", { class: "alert-title" }, `${items.length} draft${items.length === 1 ? "" : "s"} pending human review`),
          h("p", {}, "Open each draft to review, edit if necessary, and approve or reject it."))),
      h("div", { class: "queue" }, items.map((d) => h("a", { class: "queue-item", href: `#/approvals/${d.id}` },
        h("span", { class: "stat-icon", style: "width:38px;height:38px" }, icon("mail", 18)),
        h("div", { class: "grow" },
          h("div", { class: "q-title truncate" }, d.email_subject || d.subject),
          h("div", { class: "q-sub" }, `From ${senderName(d.email_sender || d.recipient)} · received ${timeAgo(d.email_received_at)}`),
          h("div", { class: "q-preview truncate" }, d.body_preview),
          d.needs_attention ? h("div", { class: "q-sub", style: "color:var(--warning)" }, d.attention_reason) : null,
        ),
        statusBadge(d.status),
        h("span", { class: "btn btn-sm btn-primary" }, "Review"),
      ))),
    );
  }

  ctx.onLeave(on("drafts", load));
  ctx.interval(load, 30000);
  await load();
}
