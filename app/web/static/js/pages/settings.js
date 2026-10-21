import { api, setAdminToken, hasAdminToken, setWorkspace } from "../api.js";
import { icon } from "../icons.js";
import { appState, changed } from "../store.js";
import { h, mount, loadingBlock, errorState, alertBox, confirmDialog, busy, notify, formatDay } from "../ui.js";
import { navigate } from "../router.js";
import { reloadApp } from "../main.js";
import { pageHead } from "./common.js";

const SECTIONS = [
  ["general", "General"], ["email", "Email"], ["knowledge", "Knowledge"], ["ai", "AI Preferences"],
  ["notifications", "Notifications"], ["security", "Security"], ["advanced", "Advanced", true],
];
const TONE_LABELS = {
  professional: "Professional — clear and courteous",
  friendly: "Friendly — warm and approachable",
  formal: "Formal — suited to official correspondence",
  concise: "Concise — brief and to the point",
};

export async function render(ctx) {
  const root = ctx.el;
  const info = appState.info;
  const visible = SECTIONS.filter(([, , admin]) => !admin || info.is_admin);
  let section = ctx.params.section || "general";
  if (!visible.some(([id]) => id === section)) section = "general";

  mount(root, pageHead("Settings", `Configuration for ${info.branding.company_name}.`), loadingBlock());
  let settings;
  try {
    settings = (await api.get("/api/settings")).settings;
  } catch (e) {
    if (ctx.alive()) mount(root, pageHead("Settings"), errorState(e.message, () => render(ctx)));
    return;
  }
  if (!ctx.alive()) return;

  const panel = h("div");
  mount(root,
    pageHead("Settings", `Configuration for ${info.branding.company_name}.`),
    h("div", { class: "settings" },
      h("nav", { class: "settings-nav", "aria-label": "Settings sections" },
        visible.map(([id, label]) => h("a", { href: `#/settings/${id}`, class: id === section ? "active" : "", "aria-current": id === section ? "page" : null }, label))),
      panel,
    ),
  );

  const save = async (patch, button) => busy(button, "Saving…", async () => {
    try {
      settings = (await api.patch("/api/settings", patch)).settings;
      notify.success("Settings saved");
      await reloadApp();
      changed("settings");
      return true;
    } catch (e) {
      notify.error("Could not save settings", e.message);
      return false;
    }
  });

  const builders = { general, email, knowledge, ai, notifications, security, advanced };
  mount(panel, h("div", { class: "card" }, loadingBlock()));
  const content = await builders[section]({ ctx, settings, save, info: appState.info, rerender: () => render(ctx) });
  if (ctx.alive()) mount(panel, content);
}

// ------------------------------------------------------------------ helpers
function card(title, description, body, footer) {
  return h("section", { class: "card" },
    h("div", { class: "card-head" }, h("div", {}, h("h2", {}, title), description ? h("p", { style: "color:var(--text-3);margin-top:2px;font-size:13px" }, description) : null)),
    h("div", { class: "card-body" }, body),
    footer ? h("div", { class: "card-foot" }, footer) : null,
  );
}
function field(label, input, hint, id) {
  if (id) input.id = id;
  return h("div", { class: "field" }, h("label", { for: input.id || null }, label), input, hint ? h("div", { class: "hint" }, hint) : null);
}
function toggle(label, description, checked) {
  const input = h("input", { type: "checkbox", checked });
  return { input, el: h("label", { class: "switch" }, input, h("span", { class: "switch-text" }, h("strong", {}, label), description ? h("span", {}, description) : null)) };
}
// Save button that is enabled only when the form differs from the saved values.
function dirtyTracker(button, read, initial) {
  let base = JSON.stringify(initial());
  const check = () => { button.disabled = JSON.stringify(read()) === base; };
  return { check, reset: () => { base = JSON.stringify(initial()); check(); } };
}

// ------------------------------------------------------------------ sections
function general({ settings, save, info }) {
  const b = settings.branding;
  const product = h("input", { class: "input", value: b.product_name, maxlength: 60, required: true });
  const company = h("input", { class: "input", value: b.company_name, maxlength: 80, required: true });
  const support = h("input", { class: "input", type: "email", value: b.support_email, placeholder: "support@yourcompany.com" });
  const color = h("input", { type: "color", value: b.primary_color });
  const colorText = h("input", { class: "input", value: b.primary_color, style: "width:120px", "aria-label": "Brand color hex value" });
  color.addEventListener("input", () => { colorText.value = color.value; tracker.check(); });
  colorText.addEventListener("input", () => { if (/^#[0-9a-f]{6}$/i.test(colorText.value)) color.value = colorText.value; tracker.check(); });

  const saveBtn = h("button", { type: "button", class: "btn btn-primary" }, "Save changes");
  const read = () => ({ product_name: product.value.trim(), company_name: company.value.trim(), support_email: support.value.trim(), primary_color: colorText.value.trim().toLowerCase() });
  const tracker = dirtyTracker(saveBtn, read, () => ({ product_name: settings.branding.product_name, company_name: settings.branding.company_name, support_email: settings.branding.support_email, primary_color: settings.branding.primary_color }));
  [product, company, support].forEach((i) => i.addEventListener("input", tracker.check));
  tracker.check();
  saveBtn.addEventListener("click", async () => {
    if (await save({ branding: read() }, saveBtn)) tracker.reset();
  });

  // logo
  const preview = h("div", { class: "logo-preview" });
  const logoInput = h("input", { type: "file", accept: ".png,.jpg,.jpeg,.svg,.webp", hidden: true });
  const uploadLogo = h("button", { type: "button", class: "btn btn-sm", onclick: () => logoInput.click() }, icon("upload", 14), "Upload logo");
  const removeLogo = h("button", { type: "button", class: "btn btn-sm btn-danger-ghost" }, "Remove");
  const drawLogo = () => {
    const has = !!appState.info.branding.logo_file;
    mount(preview, has ? h("img", { src: `/api/branding/logo?ws=${encodeURIComponent(info.workspace.id)}&v=${Date.now()}`, alt: "Current logo" }) : icon("file", 22));
    removeLogo.hidden = !has;
  };
  drawLogo();
  logoInput.addEventListener("change", () => {
    const file = logoInput.files[0];
    logoInput.value = "";
    if (!file) return;
    const fd = new FormData();
    fd.append("logo", file, file.name);
    busy(uploadLogo, "Uploading…", async () => {
      try {
        await api.upload("/api/settings/logo", fd);
        await reloadApp();
        drawLogo();
        notify.success("Logo updated");
      } catch (e) { notify.error("Upload failed", e.message); }
    });
  });
  removeLogo.addEventListener("click", async () => {
    const ok = await confirmDialog({
      title: "Remove logo?", message: "The product initials will be shown instead.", confirmLabel: "Remove", danger: true,
      busyLabel: "Removing…", action: () => api.del("/api/settings/logo"),
    });
    if (ok) { await reloadApp(); drawLogo(); notify.success("Logo removed"); }
  });

  return h("div", { class: "stack" },
    card("Branding", "How the product appears to your team.", h("div", {},
      h("div", { class: "field-row" },
        field("Product name", product, "Shown in the sidebar and browser tab.", "s-product"),
        field("Company name", company, "Used in the interface and to sign replies.", "s-company")),
      h("div", { class: "field-row" },
        field("Support email", support, "Optional. Shown to your team as the contact address.", "s-support"),
        h("div", { class: "field" }, h("span", { class: "label" }, "Brand color"), h("div", { class: "color-field" }, color, colorText))),
    ), [saveBtn]),
    card("Logo", "PNG, JPG, SVG or WebP up to 2 MB.", h("div", { style: "display:flex;gap:16px;align-items:center;flex-wrap:wrap" },
      preview, h("div", { style: "display:flex;gap:8px" }, uploadLogo, removeLogo), logoInput)),
  );
}

function email({ settings, save }) {
  const e = settings.email;
  const interval = h("select", { class: "select" },
    [[0, "Off — check manually"], [5, "Every 5 minutes"], [15, "Every 15 minutes"], [30, "Every 30 minutes"], [60, "Every hour"]]
      .concat([[e.auto_check_minutes, `Every ${e.auto_check_minutes} minutes`]].filter(([v]) => ![0, 5, 15, 30, 60].includes(v)))
      .map(([v, l]) => h("option", { value: v, selected: v === e.auto_check_minutes }, l)));
  const lookback = h("input", { class: "input", type: "number", min: 1, max: 30, value: e.lookback_days });
  const perCheck = h("input", { class: "input", type: "number", min: 1, max: 50, value: e.max_per_check });
  const promo = toggle("Skip promotional emails", "Newsletters and marketing emails are not answered.", e.skip_promotions);
  const saveBtn = h("button", { type: "button", class: "btn btn-primary" }, "Save changes");
  const read = () => ({ auto_check_minutes: Number(interval.value), lookback_days: Number(lookback.value), max_per_check: Number(perCheck.value), skip_promotions: promo.input.checked });
  const tracker = dirtyTracker(saveBtn, read, () => ({ ...settings.email }));
  [interval, lookback, perCheck, promo.input].forEach((i) => i.addEventListener("input", tracker.check));
  [interval, promo.input].forEach((i) => i.addEventListener("change", tracker.check));
  tracker.check();
  saveBtn.addEventListener("click", async () => { if (await save({ email: read() }, saveBtn)) { settings.email = read(); tracker.reset(); } });

  return h("div", { class: "stack" },
    card("Checking for new emails", null, h("div", {},
      field("Automatic checking", interval, "When on, the assistant checks connected inboxes on this schedule.", "s-interval"),
      h("div", { class: "field-row" },
        field("Look-back window (days)", lookback, "Only emails received within this many days are considered.", "s-lookback"),
        field("Emails per check", perCheck, "Maximum number of new emails processed per check.", "s-percheck")),
      h("div", { class: "field" }, promo.el),
    ), [saveBtn]),
    alertBox("info", "Replies are never sent automatically", "Every reply is saved as a Gmail draft and waits for a person to review and send it."),
  );
}

async function knowledge({ info }) {
  const reprocess = h("button", { type: "button", class: "btn" }, icon("refresh", 16), "Reprocess all documents");
  reprocess.addEventListener("click", async () => {
    let queued = 0;
    const ok = await confirmDialog({
      title: "Reprocess all documents?",
      message: "Every document will be read again. The assistant keeps working while this runs.",
      confirmLabel: "Reprocess",
      busyLabel: "Starting…",
      action: async () => { queued = (await api.post("/api/documents/reprocess-all")).queued; },
    });
    if (ok) {
      notify.info("Reprocessing started", `${queued} document(s) queued.`);
      changed("documents");
      navigate("/knowledge");
    }
  });
  return card("Knowledge", "Documents are managed in the Knowledge Base.", h("div", {},
    h("dl", { class: "info-list" },
      h("dt", {}, "Supported file types"), h("dd", {}, info.knowledge.types.join(", ")),
      h("dt", {}, "Maximum file size"), h("dd", {}, `${info.knowledge.max_upload_mb} MB per file`),
    ),
    h("div", { class: "divider" }),
    h("p", { style: "color:var(--text-2);margin-bottom:12px" }, "Reprocess documents if replies do not reflect recently changed content."),
    h("div", { style: "display:flex;gap:8px;flex-wrap:wrap" }, reprocess, h("a", { class: "btn", href: "#/knowledge" }, "Open Knowledge Base")),
  ));
}

function ai({ settings, save, info }) {
  const a = settings.ai;
  const tone = h("select", { class: "select" }, info.tones.map((t) => h("option", { value: t, selected: t === a.tone }, TONE_LABELS[t] || t)));
  const signature = h("textarea", { class: "textarea", style: "min-height:90px", placeholder: `Best regards,\n${settings.branding.company_name}`, maxlength: 500 });
  signature.value = a.signature;
  const whenUnknown = a.draft_when_unknown ? "draft" : "human";
  const radio = (value, title, text) => h("label", { class: "radio-card" },
    h("input", { type: "radio", name: "unknown", value, checked: whenUnknown === value }),
    h("span", {}, h("strong", { style: "display:block" }, title), h("span", { style: "color:var(--text-3);font-size:12.5px" }, text)));
  const radios = h("div", {},
    radio("draft", "Draft an acknowledgement", "A polite holding reply is drafted and flagged so a person can add the answer."),
    radio("human", "Leave it for a person", "No draft is created; the email is marked as needing a personal reply."));
  const saveBtn = h("button", { type: "button", class: "btn btn-primary" }, "Save changes");
  const read = () => ({ tone: tone.value, signature: signature.value.trim(), draft_when_unknown: radios.querySelector("input:checked").value === "draft" });
  const tracker = dirtyTracker(saveBtn, read, () => ({ ...settings.ai }));
  [tone, signature].forEach((i) => i.addEventListener("input", tracker.check));
  radios.addEventListener("change", tracker.check);
  tone.addEventListener("change", tracker.check);
  tracker.check();
  saveBtn.addEventListener("click", async () => { if (await save({ ai: read() }, saveBtn)) { settings.ai = read(); tracker.reset(); } });

  return card("AI Preferences", "How suggested replies are written.", h("div", {},
    field("Response tone", tone, null, "s-tone"),
    field("Signature", signature, "Added at the end of every reply. Leave empty to use your company name.", "s-signature"),
    h("div", { class: "field" }, h("span", { class: "label" }, "When the Knowledge Base has no answer"), radios),
  ), [saveBtn]);
}

function notifications() {
  const supported = "Notification" in window;
  let enabled = false;
  try { enabled = localStorage.getItem("copilot.desktopNotify") === "1"; } catch { /* ignore */ }
  const t = toggle("Desktop notifications", "Notify me in this browser when new AI drafts are waiting for review.", enabled && supported && Notification.permission === "granted");
  t.input.disabled = !supported;
  t.input.addEventListener("change", async () => {
    if (t.input.checked) {
      const permission = await Notification.requestPermission();
      if (permission !== "granted") {
        t.input.checked = false;
        notify.warning("Notifications blocked", "Allow notifications for this site in your browser settings.");
        return;
      }
    }
    try { localStorage.setItem("copilot.desktopNotify", t.input.checked ? "1" : "0"); } catch { /* ignore */ }
    notify.success(t.input.checked ? "Desktop notifications on" : "Desktop notifications off");
  });
  return h("div", { class: "stack" },
    card("Notifications", "Applies to this browser only.", h("div", {},
      t.el,
      supported ? null : h("p", { class: "hint", style: "margin-top:10px" }, "This browser does not support desktop notifications."),
    )),
    alertBox("info", null, "The bell icon at the top of the page always shows drafts waiting for review and recent activity."),
  );
}

function security({ info, rerender }) {
  const items = [
    ["No automatic sending", "The assistant can only create, update and remove drafts. People send every email."],
    ["Encrypted account access", "Email account access is stored encrypted on the server and is never shown in the browser."],
    ["Disconnect revokes access", "Disconnecting an account revokes the assistant's access with Google."],
    ["Suspicious email screening", "Emails that try to manipulate the assistant or look like phishing are flagged and not answered."],
  ];
  let adminBlock;
  if (info.is_admin) {
    const lock = h("button", { type: "button", class: "btn" }, icon("lock", 16), "Lock administrator access");
    lock.addEventListener("click", async () => { setAdminToken(""); await reloadApp(); rerender(); });
    adminBlock = h("div", {},
      h("p", {}, "You have administrator access in this browser."),
      info.admin_token_required && hasAdminToken() ? h("div", { style: "margin-top:12px" }, lock) : null,
    );
  } else if (info.admin_token_required) {
    const token = h("input", { class: "input", type: "password", autocomplete: "off", placeholder: "Administrator access key" });
    const unlock = h("button", { type: "submit", class: "btn btn-primary" }, "Unlock");
    const form = h("form", { style: "display:flex;gap:8px;flex-wrap:wrap" }, h("div", { style: "flex:1;min-width:200px" }, token), unlock);
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      busy(unlock, "Checking…", async () => {
        setAdminToken(token.value.trim());
        const res = await api.get("/api/admin/verify").catch(() => ({ is_admin: false }));
        if (!res.is_admin) { setAdminToken(""); notify.error("Access key not accepted"); return; }
        await reloadApp();
        notify.success("Administrator access unlocked");
        rerender();
      });
    });
    adminBlock = h("div", {}, h("p", { style: "margin-bottom:10px" }, "Enter the administrator access key to manage advanced settings."), form);
  } else {
    adminBlock = h("p", {}, "Advanced settings are available from the computer running the application.");
  }
  return h("div", { class: "stack" },
    card("Security", null, h("ul", { class: "steps" }, items.map(([t, d]) =>
      h("li", {}, h("span", { style: "color:var(--success)" }, icon("shieldCheck", 20)), h("span", {}, h("strong", { style: "display:block" }, t), h("span", { style: "color:var(--text-3)" }, d)))))),
    card("Administrator access", null, adminBlock),
  );
}

// AI providers: which one answers first, which stand in for it, and each one's model.
// Models are listed live by the provider; API keys stay in the server's .env file.
function aiServiceCard(ai, rerender) {
  const result = (el, ok, text) => mount(el, h("span", { class: `badge badge-${ok ? "success" : "danger"}`, style: "white-space:normal" }, text));
  const primary = h("select", { class: "select", style: "max-width:260px" },
    ai.providers.map((p) => h("option", { value: p.name, selected: p.name === ai.provider }, p.configured ? p.label : `${p.label} (not configured)`)));
  const rows = ai.providers.map((p) => {
    const model = h("select", { class: "select", style: "min-width:0;flex:1", "aria-label": `${p.label} model`, disabled: !p.configured });
    const fallback = h("input", { type: "checkbox", checked: ai.fallbacks.includes(p.name), "aria-label": `Use ${p.label} as a fallback` });
    const refresh = h("button", { type: "button", class: "btn btn-sm", title: "Reload the model list from the provider", disabled: !p.configured }, icon("refresh", 14));
    const test = h("button", { type: "button", class: "btn btn-sm", disabled: !p.configured }, "Test");
    const out = h("div", { style: "margin-top:6px" });
    const fill = (models, error) => {
      const current = p.model;
      const auto = p.default_model ? `Default from .env (${p.default_model})` : "Automatic (first model offered)";
      const names = [...new Set([...(current && !models.includes(current) ? [current] : []), ...models])];
      mount(model, h("option", { value: "" }, auto), names.map((m) => h("option", { value: m, selected: m === current }, models.includes(m) || !models.length ? m : `${m} (not offered by ${p.label})`)));
      if (error) result(out, false, error);
    };
    const load = async (refreshList) => {
      if (!p.configured) { fill([]); return; }
      mount(model, h("option", {}, "Loading models…"));
      try {
        const res = await api.get(`/api/admin/ai/models/${p.name}${refreshList ? "?refresh=1" : ""}`);
        fill(res.models, res.error);
      } catch (e) { fill([], e.message); }
    };
    refresh.addEventListener("click", () => busy(refresh, "", () => load(true)));
    test.addEventListener("click", () => busy(test, "Testing…", async () => {
      try {
        const res = await api.post("/api/admin/test-ai", { provider: p.name, model: model.value });
        result(out, res.ok, res.message);
      } catch (e) { result(out, false, e.message); }
    }));
    load(false);
    const state = p.configured ? h("span", { class: "badge badge-success" }, "Configured")
      : h("span", { class: "badge badge-warning", title: p.setup_hint }, p.needs_key ? "API key not set" : "Disabled");
    return {
      p, model, fallback,
      el: h("tr", {},
        h("td", {}, h("div", { class: "cell-title" }, p.label), h("div", { class: "cell-sub", title: p.endpoint }, p.endpoint || "—"),
          p.configured ? null : h("div", { class: "cell-sub", style: "white-space:normal" }, p.setup_hint)),
        h("td", { "data-label": "Status" }, state),
        h("td", { "data-label": "Model" }, h("div", { style: "display:inline-flex;gap:6px;align-items:center;max-width:100%" }, model, refresh, test), out),
        h("td", { "data-label": "Fallback", style: "text-align:center" }, fallback)),
    };
  });
  const syncFallbacks = () => rows.forEach((r) => { r.fallback.disabled = r.p.name === primary.value; if (r.fallback.disabled) r.fallback.checked = false; });
  primary.addEventListener("change", syncFallbacks);
  syncFallbacks();

  const saveBtn = h("button", { type: "button", class: "btn btn-primary" }, "Save AI settings");
  saveBtn.addEventListener("click", () => busy(saveBtn, "Saving…", async () => {
    try {
      await api.put("/api/admin/ai", {
        provider: primary.value,
        fallbacks: rows.filter((r) => r.fallback.checked).map((r) => r.p.name),
        models: Object.fromEntries(rows.filter((r) => r.p.configured).map((r) => [r.p.name, r.model.value])),
      });
      notify.success("AI settings saved");
      await reloadApp();
      rerender();
    } catch (e) { notify.error("Could not save AI settings", e.message); }
  }));
  const resetBtn = ai.customised ? h("button", { type: "button", class: "btn" }, "Restore .env defaults") : null;
  if (resetBtn) resetBtn.addEventListener("click", () => busy(resetBtn, "Restoring…", async () => {
    try {
      await api.put("/api/admin/ai", { reset: true });
      notify.success("AI settings restored from .env");
      await reloadApp();
      rerender();
    } catch (e) { notify.error("Could not restore AI settings", e.message); }
  }));

  const active = ai.configured
    ? h("span", {}, h("span", { class: "badge badge-success" }, "Available"), " ", ai.active.label, " · ", h("span", { class: "code" }, ai.active.model))
    : h("span", { class: "badge badge-danger" }, "No provider configured");
  return card("AI service", "Ollama runs on this server; Groq and Google Gemini are hosted. API keys are set in the server's .env file and are never shown here.", h("div", {},
    h("dl", { class: "info-list" },
      h("dt", {}, "Answering now"), h("dd", {}, active),
      h("dt", {}, "Order tried"), h("dd", {}, [ai.provider, ...ai.fallbacks].map((n) => ai.providers.find((p) => p.name === n).label).join(" → ")),
    ),
    h("div", { class: "field", style: "margin-top:16px" }, h("label", { for: "s-ai-primary" }, "Primary provider"), Object.assign(primary, { id: "s-ai-primary" }),
      h("div", { class: "hint" }, "Answers every request first. Checked fallback providers are tried in the order listed when it fails.")),
    h("div", { class: "table-wrap", style: "margin-top:12px" }, h("table", { class: "table responsive" },
      h("thead", {}, h("tr", {}, h("th", {}, "Provider"), h("th", {}, "Status"), h("th", {}, "Model"), h("th", { style: "text-align:center" }, "Fallback"))),
      h("tbody", {}, rows.map((r) => r.el)))),
  ), [resetBtn, saveBtn]);
}

async function advanced({ settings, save, info, rerender }) {
  let status;
  try {
    status = await api.get("/api/admin/status");
  } catch (e) {
    return errorState(e.message, rerender);
  }

  const aiCard = aiServiceCard(status.ai_service, rerender);

  // Gmail sign-in
  const g = status.gmail_signin;
  const clientInput = h("input", { type: "file", accept: ".json,application/json", hidden: true });
  const uploadClient = h("button", { type: "button", class: "btn", onclick: () => clientInput.click() }, icon("upload", 16), g.configured ? "Replace OAuth client file" : "Upload OAuth client file");
  clientInput.addEventListener("change", () => {
    const file = clientInput.files[0];
    clientInput.value = "";
    if (!file) return;
    const fd = new FormData();
    fd.append("client", file, file.name);
    busy(uploadClient, "Uploading…", async () => {
      try {
        await api.upload("/api/admin/oauth-client", fd);
        notify.success("Gmail sign-in configured", "Users can now connect Gmail accounts.");
        await reloadApp();
        rerender();
      } catch (e) { notify.error("Upload failed", e.message); }
    });
  });
  const copyBtn = h("button", { type: "button", class: "btn btn-sm", onclick: async () => {
    try { await navigator.clipboard.writeText(g.redirect_uri); notify.success("Copied"); } catch { notify.warning("Copy failed", "Select the address and copy it manually."); }
  } }, icon("copy", 14), "Copy");

  // Matching threshold
  const threshold = h("input", { class: "input", type: "number", min: 0, max: 0.95, step: 0.05, value: settings.advanced.min_relevance, style: "max-width:140px" });
  const saveThreshold = h("button", { type: "button", class: "btn btn-primary" }, "Save");
  const tr = dirtyTracker(saveThreshold, () => Number(threshold.value), () => settings.advanced.min_relevance);
  threshold.addEventListener("input", tr.check);
  tr.check();
  saveThreshold.addEventListener("click", async () => {
    if (await save({ advanced: { min_relevance: Number(threshold.value) } }, saveThreshold)) { settings.advanced.min_relevance = Number(threshold.value); tr.reset(); }
  });

  // Workspaces
  const wsName = h("input", { class: "input", placeholder: "New client or team name", maxlength: 80 });
  const wsCreate = h("button", { type: "submit", class: "btn btn-primary" }, icon("plus", 16), "Create workspace");
  const wsForm = h("form", { style: "display:flex;gap:8px;flex-wrap:wrap;margin-top:14px" }, h("div", { style: "flex:1;min-width:200px" }, wsName), wsCreate);
  wsForm.addEventListener("submit", (e) => {
    e.preventDefault();
    if (!wsName.value.trim()) return;
    busy(wsCreate, "Creating…", async () => {
      try {
        const ws = await api.post("/api/workspaces", { name: wsName.value.trim() });
        notify.success("Workspace created", ws.name);
        setWorkspace(ws.id);
        await reloadApp();
        navigate("/");
      } catch (err) { notify.error("Could not create workspace", err.message); }
    });
  });

  const wsRows = info.workspaces.map((w) => {
    const del = h("button", { type: "button", class: "btn btn-sm btn-danger-ghost", disabled: w.id === "default", title: w.id === "default" ? "The default workspace cannot be deleted" : null }, "Delete");
    del.addEventListener("click", async () => {
      const ok = await confirmDialog({
        title: `Delete workspace “${w.name}”?`,
        message: "All of its email accounts, knowledge documents, emails and drafts will be permanently removed from this application. Gmail drafts already in Gmail are not affected.",
        confirmLabel: "Delete workspace", busyLabel: "Deleting…", danger: true,
        action: () => api.del(`/api/workspaces/${w.id}`),
      });
      if (ok) {
        notify.success("Workspace deleted", w.name);
        if (w.id === info.workspace.id) setWorkspace("default");
        await reloadApp();
        rerender();
      }
    });
    return h("tr", {},
      h("td", {}, h("div", { class: "cell-title" }, w.name), w.id === info.workspace.id ? h("div", { class: "cell-sub" }, "Current workspace") : null),
      h("td", { class: "cell-muted" }, formatDay(w.created_at)),
      h("td", { class: "col-actions" }, del));
  });

  return h("div", { class: "stack" },
    alertBox("info", "Administrator settings", "These settings affect how the service runs. Secret keys are never shown here."),
    aiCard,
    card("Gmail sign-in", "Google OAuth client used when users connect Gmail.", h("div", {},
      h("dl", { class: "info-list" },
        h("dt", {}, "Status"), h("dd", {}, g.configured ? h("span", { class: "badge badge-success" }, "Configured") : h("span", { class: "badge badge-warning" }, "Not configured")),
        g.configured ? [h("dt", {}, "Client type"), h("dd", {}, g.client_type === "web" ? "Web application" : "Desktop app")] : null,
        g.project_id ? [h("dt", {}, "Google Cloud project"), h("dd", {}, g.project_id)] : null,
        h("dt", {}, "Redirect URI"), h("dd", {}, h("div", { style: "display:flex;gap:8px;align-items:center;flex-wrap:wrap" }, h("span", { class: "code" }, g.redirect_uri), copyBtn)),
      ),
      h("p", { class: "hint", style: "margin-top:12px;color:var(--text-3)" },
        "In Google Cloud Console enable the Gmail API, create an OAuth client (Web application) and add the redirect URI above. Then upload the downloaded client JSON file here."),
      h("div", { style: "margin-top:14px" }, uploadClient, clientInput),
    )),
    card("Knowledge storage", null, h("dl", { class: "info-list" },
      h("dt", {}, "Documents"), h("dd", {}, String(status.knowledge_storage.documents)),
      h("dt", {}, "Indexed sections"), h("dd", {}, String(status.knowledge_storage.indexed_sections)),
      h("dt", {}, "Search model"), h("dd", {}, h("span", { class: "code" }, status.knowledge_storage.search_model)),
    )),
    card("Answer matching", "Minimum match score (0–0.95) for knowledge to be used in a reply. Higher is stricter.", h("div", {},
      field("Match threshold", threshold, "Recommended: 0.30. Questions without a match get an acknowledgement or are left for a person.", "s-threshold"),
    ), [saveThreshold]),
    card("Workspaces", "Separate clients or teams. Each has its own accounts, knowledge, settings and drafts.", h("div", {},
      h("div", { class: "table-wrap" }, h("table", { class: "table" },
        h("thead", {}, h("tr", {}, h("th", {}, "Name"), h("th", {}, "Created"), h("th", { class: "col-actions" }, h("span", { class: "visually-hidden" }, "Actions")))),
        h("tbody", {}, wsRows))),
      wsForm,
    )),
  );
}
