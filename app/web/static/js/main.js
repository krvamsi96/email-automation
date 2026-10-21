import { api, getWorkspace, setWorkspace } from "./api.js";
import { icon } from "./icons.js";
import { appState, changed, on } from "./store.js";
import { route, start, onRoute, currentPath } from "./router.js";
import { h, mount, initials, timeAgo, notify } from "./ui.js";

const NAV = [
  { path: "/", label: "Home", icon: "home" },
  { section: "Knowledge" },
  { path: "/knowledge-test", label: "Knowledge Base Test", icon: "sparkle" },
  { path: "/email-test", label: "Email Test", icon: "send" },
  { path: "/knowledge", label: "Knowledge Base", icon: "book", counter: "processing" },
  { section: "Email Assistant" },
  { path: "/email-assistant", label: "Email Assistant", icon: "dashboard" },
  { path: "/emails", label: "Emails", icon: "mail" },
  { path: "/drafts", label: "Drafts", icon: "draft" },
  { path: "/approvals", label: "Approvals", icon: "approval", counter: "pending" },
  { path: "/accounts", label: "Email Accounts", icon: "account", counter: "accounts" },
  { section: "Workspace" },
  { path: "/settings", label: "Settings", icon: "settings" },
  { path: "/help", label: "Help", icon: "help" },
];

const TITLES = {
  "/": "Home", "/knowledge-test": "Knowledge Base Test", "/email-test": "Email Test", "/email-assistant": "Email Assistant", "/emails": "Emails", "/knowledge": "Knowledge Base", "/drafts": "Drafts",
  "/approvals": "Approvals", "/accounts": "Email Accounts", "/settings": "Settings", "/help": "Help",
};

route("/", () => import("./pages/home.js"));
route("/knowledge-test", () => import("./pages/knowledge-test.js"));
route("/email-test", () => import("./pages/email-test.js"));
route("/email-assistant", () => import("./pages/dashboard.js"));
route("/emails", () => import("./pages/emails.js"));
route("/emails/:id", () => import("./pages/email-detail.js"));
route("/knowledge", () => import("./pages/knowledge.js"));
route("/drafts", () => import("./pages/drafts.js"));
route("/approvals", () => import("./pages/approvals.js"));
route("/approvals/:id", () => import("./pages/review.js"));
route("/drafts/:id", () => import("./pages/review.js"));
route("/accounts", () => import("./pages/accounts.js"));
route("/settings", () => import("./pages/settings.js"));
route("/settings/:section", () => import("./pages/settings.js"));
route("/help", () => import("./pages/help.js"));

// ---------------------------------------------------------------- branding
export function applyBranding(info) {
  const b = info.branding;
  const root = document.documentElement;
  if (/^#[0-9a-f]{6}$/i.test(b.primary_color || "")) root.style.setProperty("--brand", b.primary_color);
  document.title = b.product_name;
  mount(document.getElementById("brandName"), b.product_name);
  mount(document.getElementById("brandSub"), b.company_name);
  const mark = document.getElementById("brandMark");
  if (b.logo_file) {
    mount(mark, h("img", { src: `/api/branding/logo?ws=${encodeURIComponent(info.workspace.id)}&v=${Date.now()}`, alt: "" }));
  } else {
    mount(mark, initials(b.product_name));
  }
  mount(document.getElementById("profile"),
    h("div", { class: "avatar", "aria-hidden": "true" }, initials(b.company_name)),
    h("div", { class: "profile-text" },
      h("div", { class: "profile-name" }, b.company_name),
      h("div", { class: "profile-role" }, info.is_admin ? "Administrator" : "Team member"),
    ),
  );
  renderWorkspaceSwitch(info);
}

function renderWorkspaceSwitch(info) {
  const wrap = document.getElementById("wsSwitchWrap");
  const select = document.getElementById("wsSwitch");
  const list = info.workspaces || [];
  wrap.hidden = list.length < 2;
  mount(select, list.map((w) => h("option", { value: w.id, selected: w.id === info.workspace.id }, w.name)));
}

document.getElementById("wsSwitch").addEventListener("change", async (e) => {
  setWorkspace(e.target.value);
  await reloadApp();
  location.hash = "#/";
});

export async function reloadApp() {
  appState.info = await api.get("/api/app");
  applyBranding(appState.info);
  changed("app");
  refreshCounters();
}

// ---------------------------------------------------------------- navigation
const navLinks = new Map();
function renderNav() {
  const nav = document.getElementById("nav");
  mount(nav, NAV.map((item) => {
    if (item.section) return h("div", { class: "nav-section" }, item.section);
    const count = h("span", { class: "count", hidden: true });
    const a = h("a", { href: `#${item.path}` }, icon(item.icon, 18), h("span", {}, item.label), count);
    navLinks.set(item.path, { a, count, counter: item.counter });
    return a;
  }));
}

function setActive(path) {
  const top = "/" + (path.split("/")[1] || "");
  for (const [p, { a }] of navLinks) {
    const active = p === top;
    a.classList.toggle("active", active);
    if (active) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
  mount(document.getElementById("topbarTitle"), TITLES[top] || "");
  closeSidebar();
  document.getElementById("content").focus({ preventScroll: true });
}

// ---------------------------------------------------------------- mobile sidebar
const sidebar = document.getElementById("sidebar");
const scrim = document.getElementById("scrim");
const menuBtn = document.getElementById("menuBtn");
function closeSidebar() { sidebar.classList.remove("open"); scrim.hidden = true; menuBtn.setAttribute("aria-expanded", "false"); }
menuBtn.addEventListener("click", () => {
  const open = !sidebar.classList.contains("open");
  sidebar.classList.toggle("open", open);
  scrim.hidden = !open;
  menuBtn.setAttribute("aria-expanded", String(open));
});
scrim.addEventListener("click", closeSidebar);

// ---------------------------------------------------------------- counters + notifications
let lastPending = null;
let counterTimer = null;
export async function refreshCounters() {
  clearTimeout(counterTimer);
  counterTimer = setTimeout(refreshCounters, 30000);
  let stats;
  try {
    stats = await api.get("/api/dashboard/stats");
  } catch {
    return;
  }
  const pending = stats.drafts.pending_review;
  setCount("/approvals", pending, "warning");
  setCount("/knowledge", stats.documents.processing);
  setCount("/accounts", stats.accounts.needs_attention);
  const bell = document.getElementById("bellCount");
  bell.hidden = !pending;
  bell.textContent = pending > 99 ? "99+" : String(pending);
  document.getElementById("bellBtn").setAttribute("aria-label", pending ? `Notifications, ${pending} drafts pending review` : "Notifications");
  bellData = stats;
  if (!document.getElementById("bellPanel").hidden) renderBell();

  if (lastPending !== null && pending > lastPending) desktopNotify(pending - lastPending);
  lastPending = pending;
}

function setCount(path, n) {
  const link = navLinks.get(path);
  if (!link) return;
  link.count.hidden = !n;
  link.count.textContent = String(n);
}

function desktopNotify(n) {
  let enabled = false;
  try { enabled = localStorage.getItem("copilot.desktopNotify") === "1"; } catch { /* ignore */ }
  if (!enabled || !("Notification" in window) || Notification.permission !== "granted") return;
  try {
    const n1 = new Notification(appState.info.branding.product_name, {
      body: `${n} new AI draft${n > 1 ? "s" : ""} waiting for your review.`,
    });
    n1.onclick = () => { window.focus(); location.hash = "#/approvals"; };
  } catch { /* ignore */ }
}

let bellData = null;
const bellBtn = document.getElementById("bellBtn");
const bellPanel = document.getElementById("bellPanel");
function renderBell() {
  const stats = bellData;
  const items = stats ? stats.activity.slice(0, 8) : [];
  mount(bellPanel,
    h("div", { class: "popover-head" }, "Notifications",
      stats && stats.drafts.pending_review
        ? h("a", { href: "#/approvals" }, `${stats.drafts.pending_review} pending review`) : null),
    h("div", { class: "popover-body" },
      items.length
        ? h("ul", { class: "activity" }, items.map((a) => h("li", {},
          h("div", { class: "a-text" }, h("div", { class: "a-msg" }, a.message), h("div", { class: "a-time" }, timeAgo(a.created_at))))))
        : h("div", { class: "empty", style: "padding:28px" }, h("p", {}, "No notifications yet.")),
    ),
    h("div", { class: "popover-foot" }, h("a", { href: "#/email-assistant" }, "View all activity")),
  );
}
bellBtn.addEventListener("click", (e) => {
  e.stopPropagation();
  const open = bellPanel.hidden;
  bellPanel.hidden = !open;
  bellBtn.setAttribute("aria-expanded", String(open));
  if (open) { renderBell(); refreshCounters(); }
});
document.addEventListener("click", (e) => {
  if (!bellPanel.hidden && !bellPanel.contains(e.target)) { bellPanel.hidden = true; bellBtn.setAttribute("aria-expanded", "false"); }
});
bellPanel.addEventListener("click", (e) => { if (e.target.closest("a")) { bellPanel.hidden = true; } });

// Any data change refreshes the counters (debounced).
let counterDebounce = null;
on("*", (topic) => {
  if (topic === "app") return;
  clearTimeout(counterDebounce);
  counterDebounce = setTimeout(refreshCounters, 250);
});

// ---------------------------------------------------------------- boot
async function boot() {
  try {
    try {
      appState.info = await api.get("/api/app");
    } catch (e) {
      if (e.code === "workspace_missing" && getWorkspace() !== "default") {
        setWorkspace("default");
        appState.info = await api.get("/api/app");
      } else {
        throw e;
      }
    }
  } catch (e) {
    mount(document.getElementById("boot"),
      h("div", { class: "boot-error" },
        h("h2", {}, "Unable to load the application"),
        h("p", {}, e.message),
        h("button", { class: "btn btn-primary", type: "button", onclick: () => location.reload() }, "Try again"),
      ));
    return;
  }
  renderNav();
  applyBranding(appState.info);
  document.getElementById("boot").hidden = true;
  document.getElementById("app").hidden = false;
  onRoute(setActive);
  start();
  setActive(currentPath());
  refreshCounters();
}

window.addEventListener("unhandledrejection", (e) => {
  console.error(e.reason);
  if (e.reason && e.reason.message) notify.error("Something went wrong", e.reason.message);
});

boot();
