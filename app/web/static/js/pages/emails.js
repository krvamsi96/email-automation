import { api } from "../api.js";
import { icon } from "../icons.js";
import { on } from "../store.js";
import { h, mount, loadingBlock, errorState, emptyState, statusBadge, categoryLabel, formatDateTime, senderName, senderAddress, debounce } from "../ui.js";
import { navigate } from "../router.js";
import { pageHead, checkEmailsControl } from "./common.js";

const FILTERS = [
  ["", "All statuses"], ["processed", "Processed"], ["needs_attention", "Needs Human Review"],
  ["flagged", "Security Review"], ["skipped", "Skipped"], ["failed", "Failed"], ["processing", "Processing"],
];

export async function render(ctx) {
  const root = ctx.el;
  let status = ctx.query.status || "";
  let q = ctx.query.q || "";
  let accounts = [];
  try {
    accounts = (await api.get("/api/email-accounts")).items;
  } catch { /* shown below if the list also fails */ }
  if (!ctx.alive()) return;

  const check = checkEmailsControl(ctx, { hasAccounts: accounts.length > 0, onFinished: load });
  const refreshBtn = h("button", { type: "button", class: "btn", onclick: () => load() }, icon("refresh", 16), "Refresh");
  const search = h("input", { class: "input", type: "search", placeholder: "Search sender or subject…", value: q, "aria-label": "Search emails" });
  const filter = h("select", { class: "select", "aria-label": "Filter by status", style: "width:auto" },
    FILTERS.map(([v, l]) => h("option", { value: v, selected: v === status }, l)));
  const listEl = h("div", {}, loadingBlock("Loading emails…"));

  mount(root,
    pageHead("Emails", "Incoming emails and the assistant's progress on each one.", [refreshBtn, check.button]),
    check.banner,
    h("section", { class: "card" },
      h("div", { class: "toolbar" },
        h("div", { class: "search-input grow" }, icon("search", 16), search),
        filter,
      ),
      listEl,
    ),
  );

  let seq = 0;
  async function load() {
    const mine = ++seq;
    const params = new URLSearchParams();
    if (status) params.set("status", status);
    if (q) params.set("q", q);
    let data;
    try {
      data = await api.get(`/api/emails?${params}`);
    } catch (e) {
      if (ctx.alive() && mine === seq) mount(listEl, errorState(e.message, load));
      return;
    }
    if (!ctx.alive() || mine !== seq) return;  // a newer request has been issued
    check.resume(data.job);
    draw(data.items);
  }

  function draw(items) {
    if (!items.length) {
      if (status || q) {
        mount(listEl, emptyState({ iconName: "search", title: "No matching emails", text: "Try a different search or status filter." }));
      } else if (!accounts.length) {
        mount(listEl, emptyState({
          iconName: "mail", title: "No emails available",
          text: "Connect an email account so the assistant can start preparing replies.",
          action: h("a", { class: "btn btn-primary", href: "#/accounts" }, "Connect Gmail"),
        }));
      } else {
        mount(listEl, emptyState({
          iconName: "inbox", title: "No emails available",
          text: "Use “Check for new emails” to fetch unanswered emails from your inbox.",
        }));
      }
      return;
    }
    mount(listEl, h("div", { class: "table-wrap" }, h("table", { class: "table responsive" },
      h("thead", {}, h("tr", {},
        h("th", {}, "From"), h("th", {}, "Subject"), h("th", {}, "Received"),
        h("th", {}, "Category"), h("th", {}, "Status"), h("th", {}, "AI Draft"))),
      h("tbody", {}, items.map((e) => {
        const open = () => navigate(`/emails/${e.id}`);
        return h("tr", { class: "clickable", tabindex: "0", onclick: open, onkeydown: (ev) => { if (ev.key === "Enter") open(); } },
          h("td", { "data-label": "From" },
            h("div", { class: "cell-title truncate", style: "max-width:220px" }, senderName(e.sender)),
            h("div", { class: "cell-sub", style: "max-width:220px" }, senderAddress(e.sender))),
          h("td", { "data-label": "Subject" },
            h("div", { class: "cell-title truncate", style: "max-width:380px" }, e.subject),
            h("div", { class: "cell-sub" }, e.snippet)),
          h("td", { "data-label": "Received", class: "cell-muted" }, formatDateTime(e.received_at)),
          h("td", { "data-label": "Category" }, e.category ? h("span", { class: "chip" }, categoryLabel(e.category)) : h("span", { class: "cell-muted" }, "—")),
          h("td", { "data-label": "Status" }, statusBadge(e.status)),
          h("td", { "data-label": "AI Draft" }, e.draft_status
            ? h("a", { href: `#/drafts/${e.draft_id}`, onclick: (ev) => ev.stopPropagation() }, statusBadge(e.draft_status))
            : h("span", { class: "cell-muted" }, "—")),
        );
      })),
    )));
  }

  const syncUrl = () => {
    const params = new URLSearchParams();
    if (status) params.set("status", status);
    if (q) params.set("q", q);
    history.replaceState(null, "", `#/emails${params.toString() ? "?" + params : ""}`);
  };
  search.addEventListener("input", debounce(() => { q = search.value.trim(); syncUrl(); load(); }, 300));
  filter.addEventListener("change", () => { status = filter.value; syncUrl(); load(); });
  ctx.onLeave(on("emails", load));
  ctx.interval(load, 30000);
  await load();
}
