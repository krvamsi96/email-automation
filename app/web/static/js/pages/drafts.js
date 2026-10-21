import { api } from "../api.js";
import { icon } from "../icons.js";
import { changed, on } from "../store.js";
import { h, mount, loadingBlock, errorState, emptyState, statusBadge, formatDateTime, senderName, busy, notify } from "../ui.js";
import { navigate } from "../router.js";
import { pageHead } from "./common.js";

const TABS = [
  ["", "All", "total"], ["awaiting_review", "Pending Review", "pending_review"], ["approved", "Approved", "approved"],
  ["sent", "Sent", "sent"], ["rejected", "Rejected", "rejected"], ["failed", "Failed", "failed"],
];

export async function render(ctx) {
  const root = ctx.el;
  let tab = ctx.query.status || "";
  const syncBtn = h("button", { type: "button", class: "btn" }, icon("refresh", 16), "Refresh from Gmail");
  const tabsEl = h("div", { class: "tabs", role: "tablist" });
  const listEl = h("div", {}, loadingBlock("Loading drafts…"));
  mount(root,
    pageHead("Drafts", "Every AI-generated reply and where it is in the review process.", [syncBtn]),
    h("section", { class: "card" }, tabsEl, listEl),
  );

  let seq = 0;
  async function load() {
    const mine = ++seq;
    let data;
    try {
      data = await api.get(`/api/drafts${tab ? `?status=${tab}` : ""}`);
    } catch (e) {
      if (ctx.alive() && mine === seq) mount(listEl, errorState(e.message, load));
      return;
    }
    if (!ctx.alive() || mine !== seq) return;
    drawTabs(data.stats);
    draw(data.items);
  }

  function drawTabs(stats) {
    const total = stats.total + stats.failed;
    mount(tabsEl, TABS.map(([value, label, key]) => h("button", {
      type: "button", role: "tab", class: `tab${value === tab ? " active" : ""}`, "aria-selected": String(value === tab),
      onclick: () => { tab = value; history.replaceState(null, "", `#/drafts${tab ? "?status=" + tab : ""}`); mount(listEl, loadingBlock()); load(); },
    }, label, h("span", { class: "tab-count" }, String(key === "total" ? total : stats[key] || 0)))));
  }

  function draw(items) {
    if (!items.length) {
      mount(listEl, emptyState({
        iconName: "draft",
        title: tab === "awaiting_review" ? "No drafts waiting for review" : "No drafts yet",
        text: tab ? "Nothing in this category right now." : "AI drafts appear here after new emails are processed.",
      }));
      return;
    }
    mount(listEl, h("div", { class: "table-wrap" }, h("table", { class: "table responsive" },
      h("thead", {}, h("tr", {}, h("th", {}, "Recipient"), h("th", {}, "Subject"), h("th", {}, "Created"), h("th", {}, "Status"),
        h("th", { class: "col-actions" }, h("span", { class: "visually-hidden" }, "Actions")))),
      h("tbody", {}, items.map((d) => {
        const open = () => navigate(`/drafts/${d.id}`);
        return h("tr", { class: "clickable", tabindex: "0", onclick: open, onkeydown: (e) => { if (e.key === "Enter") open(); } },
          h("td", { "data-label": "Recipient" }, h("div", { class: "cell-title truncate", style: "max-width:220px" }, senderName(d.recipient))),
          h("td", { "data-label": "Subject" },
            h("div", { class: "cell-title truncate", style: "max-width:400px" }, d.subject),
            h("div", { class: "cell-sub" }, d.body_preview)),
          h("td", { "data-label": "Created", class: "cell-muted" }, formatDateTime(d.created_at)),
          h("td", { "data-label": "Status" },
            h("div", { style: "display:flex;gap:6px;align-items:center;flex-wrap:wrap" }, statusBadge(d.status),
              d.needs_attention && ["pending_review", "edited"].includes(d.status) ? h("span", { title: d.attention_reason, style: "color:var(--warning)" }, icon("warning", 16)) : null)),
          h("td", { class: "col-actions" },
            d.status === "failed" ? h("button", { type: "button", class: "btn btn-sm", onclick: (e) => { e.stopPropagation(); retry(d, e.currentTarget); } }, "Retry")
              : ["pending_review", "edited"].includes(d.status) ? h("a", { class: "btn btn-sm btn-primary", href: `#/drafts/${d.id}`, onclick: (e) => e.stopPropagation() }, "Review")
              : h("a", { class: "btn btn-sm", href: `#/drafts/${d.id}`, onclick: (e) => e.stopPropagation() }, "View")),
        );
      })),
    )));
  }

  async function retry(d, button) {
    await busy(button, "Creating draft…", async () => {
      try {
        await api.post(`/api/drafts/${d.id}/retry`);
        notify.success("Draft saved to Gmail", "It is now waiting for review.");
        changed("drafts", "emails");
      } catch (e) {
        notify.error("Could not create the draft", e.message);
      }
    });
    load();
  }

  syncBtn.addEventListener("click", () => busy(syncBtn, "Checking Gmail…", async () => {
    try {
      const res = await api.post("/api/drafts/sync");
      const parts = [];
      if (res.sent) parts.push(`${res.sent} sent`);
      if (res.rejected) parts.push(`${res.rejected} removed in Gmail`);
      if (res.errors) notify.warning("Some drafts could not be checked", "Reconnect the email account and try again.");
      notify.info("Drafts updated", parts.length ? parts.join(", ") + "." : "No changes found in Gmail.");
      changed("drafts");
    } catch (e) {
      notify.error("Could not refresh from Gmail", e.message);
    }
    await load();
  }));

  ctx.onLeave(on("drafts", load));
  await load();
}
