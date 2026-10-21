// Home: choose between the Knowledge Base Test (no Gmail) and the Email Assistant (Gmail).
import { api } from "../api.js";
import { icon, gmailLogo } from "../icons.js";
import { appState, on } from "../store.js";
import { h, mount, loadingBlock, errorState } from "../ui.js";
import { emailTestPanel } from "./email-test.js";

export async function render(ctx) {
  mount(ctx.el, loadingBlock());
  let stats, accounts;
  try {
    [stats, accounts] = await Promise.all([api.get("/api/dashboard/stats"), api.get("/api/email-accounts")]);
  } catch (e) {
    if (ctx.alive()) mount(ctx.el, errorState(e.message, () => render(ctx)));
    return;
  }
  if (!ctx.alive()) return;
  // Created once so typed input and results survive status refreshes.
  const testPanel = emailTestPanel(ctx);
  draw(stats, accounts.items);
  ctx.onLeave(on("*", async (topic) => {
    if (topic === "app") return;
    try {
      const [s, a] = await Promise.all([api.get("/api/dashboard/stats"), api.get("/api/email-accounts")]);
      if (ctx.alive()) draw(s, a.items);
    } catch { /* keep the current view */ }
  }));

  function draw(stats, accounts) {
    const info = appState.info;
    const docs = stats.documents;
    const connected = accounts.filter((a) => a.status === "connected");
    const pending = stats.drafts.pending_review;

    const status = (ok, label, value) => h("div", { class: "sys-item" },
      h("span", { class: `sys-dot ${ok === true ? "ok" : ok === false ? "off" : "warn"}` }), h("span", { class: "sys-label" }, label), h("strong", {}, value));

    mount(ctx.el,
      h("section", { class: "hero" },
        h("h1", {}, info.branding.product_name),
        h("p", { class: "hero-lead" }, "An AI assistant that answers questions from your company's documents and prepares email replies for your team to review."),
        h("h2", { class: "hero-q" }, "What would you like to do?"),
      ),
      h("div", { class: "modes" },
        modeCard({
          iconEl: icon("book", 26), tone: "kb",
          title: "Knowledge Base Test",
          text: "Upload documents and ask questions to see how the AI answers — without connecting Gmail.",
          facts: [
            [docs.ready ? "ok" : "warn", docs.ready ? `${docs.ready} document${docs.ready === 1 ? "" : "s"} ready` : docs.processing ? "Documents processing…" : "No documents yet"],
            ["ok", "Gmail not required"],
          ],
          cta: h("a", { class: "btn btn-primary btn-lg", href: "#/knowledge-test" }, "Test Knowledge"),
        }),
        modeCard({
          iconEl: gmailLogo(26), tone: "mail",
          title: "Email Assistant",
          text: "Connect Gmail and let the AI prepare reply drafts for incoming emails. You review and send them.",
          facts: [
            [connected.length ? "ok" : "off", connected.length ? `Gmail connected (${connected.map((a) => a.email).join(", ")})` : "Gmail not connected"],
            [pending ? "warn" : "ok", pending ? `${pending} draft${pending === 1 ? "" : "s"} waiting for review` : "No drafts waiting"],
          ],
          cta: connected.length
            ? h("a", { class: "btn btn-primary btn-lg", href: "#/email-assistant" }, "Open Email Assistant")
            : h("a", { class: "btn btn-primary btn-lg", href: "#/accounts?connect=1" }, gmailLogo(16), "Connect Gmail"),
        }),
      ),
      h("section", { class: "sys-status card", "aria-label": "Status" },
        status(docs.ready > 0 ? true : null, "Knowledge Base", docs.ready > 0 ? "Ready ✓" : "Add documents"),
        status(docs.ready > 0 ? true : null, "Knowledge Test", docs.ready > 0 ? "Available ✓" : "Needs a document"),
        status(connected.length ? true : false, "Gmail", connected.length ? "Connected ✓" : "Disconnected"),
        status(info.ai_available ? true : false, "AI Service", info.ai_available ? `${info.ai_provider} ✓` : "Not set up"),
      ),
      h("section", { style: "margin-top:24px" },
        h("div", { class: "page-head", style: "margin-bottom:14px" },
          h("div", {}, h("h1", { style: "font-size:19px" }, "Email Test"),
            h("p", {}, "Type an email below and see the AI's output instantly — no Gmail needed."))),
        testPanel),
    );
  }
}

function modeCard({ iconEl, tone, title, text, facts, cta }) {
  return h("section", { class: `mode-card mode-${tone}` },
    h("div", { class: "mode-icon" }, iconEl),
    h("h2", {}, title),
    h("p", { class: "mode-text" }, text),
    h("ul", { class: "mode-facts" }, facts.map(([tone, label]) => h("li", {}, h("span", { class: `sys-dot ${tone}` }), label))),
    h("div", { class: "mode-cta" }, cta),
  );
}
