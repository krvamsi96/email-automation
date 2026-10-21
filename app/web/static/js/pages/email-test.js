// Email Test: type an email (from, subject, body) and see what the assistant would reply.
// Runs the real email workflow without Gmail; no draft is created.
import { api } from "../api.js";
import { icon } from "../icons.js";
import { h, mount, busy, notify, alertBox, categoryLabel } from "../ui.js";
import { pageHead } from "./common.js";

const STEPS = [
  ["security", "Security Check", "shieldCheck"],
  ["understand", "Understand Email", "mail"],
  ["knowledge", "Knowledge Search", "search"],
  ["reply", "AI Reply", "sparkle"],
  ["quality", "Quality Check", "check"],
];

const OUTCOME = {
  processing: ["Processing…", "info"],
  reply_ready: ["Reply Ready", "success"],
  no_knowledge: ["No Knowledge Found", "warning"],
  flagged: ["Security Risk", "danger"],
  skipped: ["No Reply Needed", "neutral"],
  ai_unavailable: ["AI Unavailable", "danger"],
  failed: ["Failed", "danger"],
};

export async function render(ctx) {
  mount(ctx.el,
    pageHead("Email Test", "Type an email and instantly see how the assistant understands it and what reply it writes. No Gmail needed."),
    emailTestPanel(ctx));
}

export function emailTestPanel(ctx) {
  const from = h("input", { class: "input", placeholder: "customer@example.com", maxlength: 200, "aria-label": "From" });
  const subject = h("input", { class: "input", placeholder: "e.g. Password reset help", maxlength: 300, "aria-label": "Subject" });
  const body = h("textarea", { class: "textarea", style: "min-height:180px", placeholder: "Type the email message here…", "aria-label": "Message" });
  const runBtn = h("button", { type: "submit", class: "btn btn-primary btn-lg" }, icon("sparkle", 16), "Generate Reply");
  const clearBtn = h("button", { type: "button", class: "btn btn-ghost" }, "Clear");
  const output = h("div", { class: "et-output" }, idle());
  let stop = null;

  const form = h("form", { class: "card et-input" },
    h("div", { class: "card-head" }, h("h2", {}, h("span", { class: "io-tag" }, "INPUT"), " Email")),
    h("div", { class: "card-body" },
      h("div", { class: "field" }, h("label", {}, "From"), from),
      h("div", { class: "field" }, h("label", {}, "Subject"), subject),
      h("div", { class: "field" }, h("label", {}, "Message"), body),
    ),
    h("div", { class: "card-foot" }, clearBtn, runBtn),
  );

  clearBtn.addEventListener("click", () => {
    from.value = ""; subject.value = ""; body.value = "";
    if (stop) { stop(); stop = null; }
    runBtn.disabled = false;
    mount(output, idle());
  });

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    if (!subject.value.trim() && !body.value.trim()) { notify.warning("Enter a subject or message"); body.focus(); return; }
    busy(runBtn, "Starting…", async () => {
      try {
        const run = await api.post("/api/email-test", { sender: from.value, subject: subject.value, body: body.value });
        draw(run);
        follow(run.id);
      } catch (err) {
        notify.error("Could not run the test", err.message);
      }
    });
  });

  function follow(id) {
    if (stop) stop();
    runBtn.disabled = true;
    stop = ctx.interval(async () => {
      let r;
      try { r = await api.get(`/api/email-test/${id}`); } catch { return; }
      draw(r);
      if (r.status !== "processing") { stop(); stop = null; runBtn.disabled = false; }
    }, 1000);
  }

  function draw(r) { mount(output, result(r)); }

  return h("div", { class: "et-grid" }, form, output);
}

function idle() {
  return h("section", { class: "card" },
    h("div", { class: "card-head" }, h("h2", {}, h("span", { class: "io-tag io-tag-out" }, "OUTPUT"), " AI Result")),
    h("div", { class: "empty" },
      h("div", { class: "empty-icon" }, icon("sparkle", 26)),
      h("h3", {}, "Your result will appear here"),
      h("p", {}, "Enter an email on the left and click Generate Reply. You'll see the security check, what the customer is asking, the knowledge used and the suggested reply.")));
}

function result(r) {
  const [label, tone] = OUTCOME[r.status] || [r.status, "neutral"];
  const processing = r.status === "processing";
  const out = r.output || {};
  const done = new Set(r.steps || []);

  const flow = h("ol", { class: "flow", "aria-label": "Processing steps" }, STEPS.map(([key, title, ic], i) => {
    let st = done.has(key) ? "done" : "wait";
    if (processing && !done.has(key) && STEPS.findIndex(([k]) => !done.has(k)) === i) st = "active";
    if (!processing && !done.has(key)) st = "skip";
    if (key === "security" && out.security && !out.security.safe) st = "fail";
    return h("li", { class: `flow-step flow-${st}` },
      h("span", { class: "flow-dot" }, st === "active" ? h("span", { class: "spinner" }) : icon(st === "done" ? "check" : st === "fail" ? "warning" : ic, 16)),
      h("span", { class: "flow-title" }, title),
      h("span", { class: "flow-state" }, { done: "Done", active: "In progress", wait: "Waiting", skip: "Not needed", fail: "Risk found" }[st]),
      i < STEPS.length - 1 ? h("span", { class: "flow-line", "aria-hidden": "true" }) : null);
  }));

  const blocks = [];
  if (processing) {
    blocks.push(h("div", { class: "io-block", style: "display:flex;gap:10px;align-items:center;color:var(--text-2)" }, h("span", { class: "spinner" }), r.stage || "Working…"));
  } else if (r.status === "failed") {
    blocks.push(alertBox("danger", "Something went wrong", r.message || "Please try again."));
  } else {
    blocks.push(h("div", { class: "et-facts" },
      fact("Security", out.security.safe ? h("span", { class: "badge badge-success" }, "Safe") : h("span", { class: "badge badge-danger" }, "Suspicious")),
      fact("Category", h("span", { class: "chip" }, categoryLabel(out.category))),
      fact("Quality", out.quality && out.quality.score ? h("span", { class: `badge badge-${out.quality.passed ? "success" : "warning"}` }, `${out.quality.score}/10`) : h("span", { class: "cell-muted" }, "—")),
    ));
    if (!out.security.safe) blocks.push(alertBox("danger", "Security risk detected", `${out.security.reason || "This email looks suspicious."} No reply is written for suspicious emails.`));
    if (r.status === "skipped") blocks.push(alertBox("info", "No reply needed", "This looks like a promotional or unrelated email."));
    if (out.question) blocks.push(h("div", { class: "io-block" }, h("div", { class: "io-label" }, "Detected Request"), h("div", { class: "io-question" }, out.question)));
    if (out.security.safe && r.status !== "skipped") {
      blocks.push(h("div", { class: "io-block" }, h("div", { class: "io-label" }, "Knowledge Used"),
        out.sources.length
          ? h("div", { class: "stack" }, out.sources.map((s) => h("div", { class: "source-card" },
              h("div", { class: "source-name" }, icon("file", 16), s.document),
              s.excerpts.slice(0, 2).map((x) => h("div", { class: "excerpt" }, x.page ? h("span", { class: "chip", style: "margin-right:6px" }, `Page ${x.page}`) : null, x.text)))))
          : h("dl", { class: "info-list" },
              h("dt", {}, "Result"), h("dd", {}, h("strong", {}, "No reliable information found.")),
              h("dt", {}, "Recommended Action"), h("dd", {}, h("span", { class: "badge badge-warning" }, "Human Review")))));
    }
    if (out.reply) {
      const copy = h("button", { type: "button", class: "btn btn-sm" }, icon("copy", 14), "Copy Reply");
      copy.addEventListener("click", async () => {
        try { await navigator.clipboard.writeText(out.reply); notify.success("Reply copied"); } catch { notify.warning("Copy failed"); }
      });
      blocks.push(h("div", { class: "io-block io-output" },
        h("div", { class: "io-label", style: "display:flex;justify-content:space-between;align-items:center" }, "Suggested Reply", copy),
        r.status === "no_knowledge" ? h("p", { class: "hint", style: "color:var(--warning);margin-bottom:8px" }, "No answer was found in your documents, so a polite acknowledgement was written instead.") : null,
        r.status === "ai_unavailable" ? h("p", { class: "hint", style: "color:var(--danger);margin-bottom:8px" }, "AI service is temporarily unavailable, so a holding reply was written. Please try again.") : null,
        h("div", { class: "email-body" }, out.reply)));
    }
  }

  return h("section", { class: "card" },
    h("div", { class: "card-head" }, h("h2", {}, h("span", { class: "io-tag io-tag-out" }, "OUTPUT"), " AI Result"),
      h("span", { class: `badge badge-${tone}${processing ? " badge-processing" : ""}` }, label)),
    h("div", { class: "card-body" }, flow, h("div", { class: "io-grid" }, blocks)));
}

function fact(label, value) {
  return h("div", { class: "et-fact" }, h("div", { class: "io-label", style: "margin-bottom:4px" }, label), value);
}
