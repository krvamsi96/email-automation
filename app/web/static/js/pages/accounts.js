import { api } from "../api.js";
import { icon, gmailLogo } from "../icons.js";
import { appState, changed } from "../store.js";
import { h, mount, loadingBlock, errorState, emptyState, statusBadge, formatDay, timeAgo, alertBox, modal, confirmDialog, busy, notify } from "../ui.js";
import { pageHead } from "./common.js";

export async function render(ctx) {
  const root = ctx.el;
  const addBtn = h("button", { type: "button", class: "btn btn-primary", onclick: () => openConnect() }, icon("plus", 16), "Add Email Account");
  const bannerEl = h("div");
  const listEl = h("div", {}, loadingBlock("Loading accounts…"));
  mount(root,
    pageHead("Email Accounts", "Inboxes the assistant reads and prepares draft replies for.", [addBtn]),
    bannerEl,
    h("section", { class: "card" }, h("div", { class: "card-head" }, h("h2", {}, "Connected Accounts")), listEl),
    h("section", { class: "card" },
      h("div", { class: "card-head" }, h("h2", {}, "What the assistant can do with your inbox")),
      h("div", { class: "card-body" },
        h("ul", { class: "steps" },
          step("✓", "Read incoming emails that have not been answered yet."),
          step("✓", "Save suggested replies as drafts in the same conversation."),
          step("✕", "Send emails. Every reply stays a draft until a person sends it from Gmail."),
        ),
      ),
    ),
  );

  // Result of the Google sign-in redirect.
  const q = ctx.query;
  if (q.oauth) {
    history.replaceState(null, "", "#/accounts");
    if (q.oauth === "connected") {
      mount(bannerEl, h("div", { class: "mb" }, alertBox("success", "Connected", `${q.account || "Your account"} is connected and verified.`)));
      notify.success("Email account connected", q.account || "");
      changed("accounts");
    } else if (q.oauth === "cancelled") {
      mount(bannerEl, h("div", { class: "mb" }, alertBox("warning", "Connection cancelled", "Google sign-in was cancelled. No access was granted.",
        [h("button", { type: "button", class: "btn btn-sm", onclick: () => openConnect() }, "Try again")])));
    } else {
      mount(bannerEl, h("div", { class: "mb" }, alertBox("danger", "Gmail connection failed", q.message || "Please try connecting again.",
        [h("button", { type: "button", class: "btn btn-sm", onclick: () => openConnect() }, "Reconnect")])));
    }
  }

  let accounts = [];
  let signinAvailable = appState.info.gmail_signin_available;
  async function load() {
    let data;
    try {
      data = await api.get("/api/email-accounts");
    } catch (e) {
      if (ctx.alive()) mount(listEl, errorState(e.message, load));
      return;
    }
    if (!ctx.alive()) return;
    accounts = data.items;
    signinAvailable = data.signin_available;
    draw();
  }

  function draw() {
    if (!accounts.length) {
      mount(listEl, emptyState({
        iconName: "account", title: "No email accounts connected",
        text: "Connect a Gmail inbox so the assistant can start preparing replies.",
        action: h("button", { type: "button", class: "btn btn-primary", onclick: () => openConnect() }, "Connect Gmail"),
      }));
      return;
    }
    mount(listEl, accounts.map(accountRow));
  }

  function accountRow(a) {
    const testBtn = h("button", { type: "button", class: "btn btn-sm" }, "Test Connection");
    const disconnectBtn = h("button", { type: "button", class: "btn btn-sm btn-danger-ghost" }, "Disconnect");
    const reconnectBtn = a.status !== "connected"
      ? h("button", { type: "button", class: "btn btn-sm btn-primary", onclick: () => openConnect() }, "Reconnect") : null;

    testBtn.addEventListener("click", () => busy(testBtn, "Testing…", async () => {
      try {
        const res = await api.post(`/api/email-accounts/${a.id}/test`);
        notify.success("Connection verified", `${res.email} is working. ${res.messages_total.toLocaleString()} messages in the mailbox.`);
      } catch (e) {
        notify.error("Gmail connection failed", e.message);
      }
      changed("accounts");
      await load();
    }));

    disconnectBtn.addEventListener("click", async () => {
      const ok = await confirmDialog({
        title: "Disconnect this account?",
        message: h("div", {},
          h("p", {}, h("strong", {}, a.email)),
          h("p", { style: "margin-top:8px" }, "The assistant will stop reading this inbox and its access will be revoked. Existing Gmail drafts stay in Gmail."),
        ),
        confirmLabel: "Disconnect",
        busyLabel: "Disconnecting…",
        danger: true,
        action: () => api.del(`/api/email-accounts/${a.id}`),
      });
      if (ok) {
        notify.success("Account disconnected", a.email);
        changed("accounts");
        load();
      }
    });

    return h("div", { class: "account" },
      h("div", { class: "account-logo" }, gmailLogo(24)),
      h("div", { class: "grow" },
        h("div", { style: "display:flex;gap:10px;align-items:center;flex-wrap:wrap" }, h("span", { class: "account-email" }, a.email), statusBadge(a.status)),
        h("div", { class: "account-meta" },
          `Gmail · connected ${formatDay(a.connected_at)}`,
          a.last_checked_at ? ` · last checked ${timeAgo(a.last_checked_at)}` : " · not checked yet"),
        a.status_message ? h("div", { class: "cell-error" }, a.status_message) : null,
      ),
      h("div", { class: "account-actions" }, reconnectBtn, testBtn, disconnectBtn),
    );
  }

  function openConnect() {
    if (!signinAvailable) {
      const m = modal({
        title: "Gmail sign-in is not set up yet",
        body: h("div", { class: "stack" },
          h("p", {}, "Connecting Gmail requires a one-time setup by your administrator."),
          appState.info.is_admin
            ? h("p", {}, "As an administrator, you can complete it in Settings → Advanced.")
            : h("p", {}, "Please ask your administrator to complete the Gmail sign-in setup."),
        ),
        actions: [
          h("button", { type: "button", class: "btn", onclick: () => m.close() }, "Close"),
          appState.info.is_admin ? h("a", { class: "btn btn-primary", href: "#/settings/advanced", onclick: () => m.close() }, "Open Advanced Settings") : null,
        ].filter(Boolean),
      });
      return;
    }
    const cont = h("button", { type: "button", class: "btn btn-primary" }, gmailLogo(16), "Continue with Google");
    const err = h("p", { class: "form-error", hidden: true, role: "alert" });
    const m = modal({
      title: "Connect Gmail",
      wide: true,
      body: h("div", { class: "stack" },
        h("p", {}, "You will be taken to Google to sign in and allow access. Then you'll return here automatically."),
        h("ol", { class: "steps" },
          step("1", "Sign in to the Google account you want to connect."),
          step("2", "Allow permission to read emails and manage drafts."),
          step("3", "We verify the connection and you're done."),
        ),
        alertBox("info", null, "The assistant never sends emails. Replies are saved as drafts for you to review."),
        err,
      ),
      actions: [h("button", { type: "button", class: "btn", onclick: () => m.close() }, "Cancel"), cont],
    });
    cont.addEventListener("click", () => busy(cont, "Connecting…", async () => {
      err.hidden = true;
      try {
        const res = await api.post("/api/email-accounts/connect");
        window.location.assign(res.auth_url);
        await new Promise(() => {}); // keep the loading state while the browser navigates
      } catch (e) {
        err.textContent = e.message;
        err.hidden = false;
      }
    }));
  }

  await load();
  // Arriving from a "Connect Gmail" button elsewhere opens the connect dialog directly.
  if (ctx.query.connect && ctx.alive()) {
    history.replaceState(null, "", "#/accounts");
    openConnect();
  }
}

function step(n, text) {
  return h("li", {}, h("span", { class: "step-num" }, n), h("span", {}, text));
}
