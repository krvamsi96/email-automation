// Knowledge Base Test: ask questions against the Knowledge Base. Gmail is not needed.
import { api } from "../api.js";
import { icon } from "../icons.js";
import { h, mount, statusBadge, alertBox, busy, notify, timeAgo, emptyState, confirmDialog } from "../ui.js";
import { pageHead } from "./common.js";
import { documentsPanel } from "../components/documents.js";

const FLOW = [
  ["input", "Your Question", "help"],
  ["search", "Knowledge Search", "search"],
  ["found", "Relevant Information", "book"],
  ["ai", "AI Processing", "sparkle"],
  ["output", "Answer", "check"],
];

export async function render(ctx) {
  let readyDocs = 0;
  let current = null;
  let pollStop = null;
  let followUp = null; // earlier test the next question follows up on

  const questionInput = h("textarea", { class: "textarea", style: "min-height:96px", placeholder: "Type your question here…", maxlength: 1000, "aria-label": "Your question" });
  const askBtn = h("button", { type: "submit", class: "btn btn-primary btn-lg" }, icon("sparkle", 16), "Generate Answer");
  const askHint = h("p", { class: "hint", style: "color:var(--text-3);margin-top:8px" });
  const resultEl = h("div", { id: "kt-result" });
  const followEl = h("div", { style: "margin-bottom:8px" });
  const historyEl = h("div");

  const docs = documentsPanel(ctx, {
    compact: true,
    onChange: (stats) => {
      readyDocs = stats.ready;
      askBtn.disabled = readyDocs === 0 || !!pollStop;
      askHint.textContent = readyDocs === 0
        ? (stats.processing ? "Your documents are being processed. You can ask once one is Ready." : "Upload at least one document first.")
        : `Answers come only from your ${readyDocs} ready document${readyDocs === 1 ? "" : "s"}.`;
    },
  });

  const form = h("form", {}, followEl, questionInput, h("div", { style: "display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-top:12px" }, askBtn, askHint));

  mount(ctx.el,
    pageHead("Knowledge Base Test", "Upload documents and ask questions to see exactly how the AI answers. No Gmail connection needed.", [
      h("span", { class: "chip" }, icon("shieldCheck", 14), "Gmail not required"),
    ]),
    h("section", { class: "step-block" },
      stepHeader(1, "Add Knowledge", "Upload the documents the AI should learn from. They become searchable knowledge."),
      docs.el),
    stepCard(2, "Ask a Question", "Ask anything your customers or colleagues might ask.", h("div", { class: "card-body" }, form)),
    resultEl,
    h("section", { class: "card", style: "margin-top:20px" },
      h("div", { class: "card-head" }, h("h2", {}, "Test History"),
        h("button", { type: "button", class: "btn btn-sm btn-ghost", onclick: clearHistory }, "Clear history")),
      historyEl),
  );

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const q = questionInput.value.trim();
    if (!q) { notify.warning("Please type a question"); questionInput.focus(); return; }
    busy(askBtn, "Starting…", async () => {
      try {
        const body = { question: q };
        if (followUp) body.follow_up_to = followUp.id;
        const test = await api.post("/api/knowledge/ask", body);
        setFollowUp(null);
        show(test);
        follow(test.id);
      } catch (err) {
        notify.error("Could not generate an answer", err.message);
      }
    });
  });

  function follow(id) {
    if (pollStop) pollStop();
    askBtn.disabled = true;
    pollStop = ctx.interval(async () => {
      let t;
      try { t = await api.get(`/api/knowledge/tests/${id}`); } catch { return; }
      show(t);
      if (t.status !== "processing") {
        pollStop(); pollStop = null;
        askBtn.disabled = readyDocs === 0;
        loadHistory();
      }
    }, 1000);
  }

  function setFollowUp(test) {
    followUp = test;
    if (!test) { followEl.replaceChildren(); return; }
    mount(followEl, h("span", { class: "chip" }, icon("sparkle", 14), "Follow-up to: ",
      h("span", { class: "truncate", style: "max-width:360px;display:inline-block;vertical-align:bottom" }, test.question),
      h("button", { type: "button", class: "btn btn-sm btn-ghost", "aria-label": "Ask a new question instead", onclick: () => setFollowUp(null) }, icon("close", 12))));
    questionInput.value = "";
    questionInput.focus();
  }

  function show(test) {
    current = test;
    mount(resultEl, resultView(test, () => { questionInput.value = test.question; form.requestSubmit(); },
      () => { setFollowUp(test); questionInput.scrollIntoView({ behavior: "smooth", block: "center" }); }));
    if (test.status === "processing" && !resultEl.dataset.scrolled) {
      resultEl.dataset.scrolled = "1";
      resultEl.scrollIntoView({ behavior: "smooth", block: "start" });
    }
    if (test.status !== "processing") delete resultEl.dataset.scrolled;
  }

  async function loadHistory() {
    let items;
    try { items = (await api.get("/api/knowledge/tests")).items; } catch (e) {
      mount(historyEl, h("div", { class: "card-body" }, h("p", { class: "form-error" }, e.message)));
      return;
    }
    if (!ctx.alive()) return;
    if (!items.length) {
      mount(historyEl, emptyState({ iconName: "sparkle", title: "No questions asked yet", text: "Your recent tests will appear here." }));
      return;
    }
    mount(historyEl, h("div", { class: "table-wrap" }, h("table", { class: "table responsive" },
      h("thead", {}, h("tr", {}, h("th", {}, "Question"), h("th", {}, "Result"), h("th", {}, "Source"), h("th", {}, "Time"))),
      h("tbody", {}, items.map((t) => {
        const open = () => { show(t); if (t.status === "processing") follow(t.id); resultEl.scrollIntoView({ behavior: "smooth" }); };
        return h("tr", { class: "clickable", tabindex: "0", onclick: open, onkeydown: (e) => { if (e.key === "Enter") open(); } },
          h("td", { "data-label": "Question" }, h("div", { class: "cell-title truncate", style: "max-width:420px" }, t.question),
            t.answer ? h("div", { class: "cell-sub" }, t.answer) : null),
          h("td", { "data-label": "Result" }, statusBadge(t.status)),
          h("td", { "data-label": "Source", class: "cell-muted" }, t.sources.map((s) => s.document).join(", ") || "—"),
          h("td", { "data-label": "Time", class: "cell-muted" }, timeAgo(t.created_at)));
      })),
    )));
  }

  async function clearHistory() {
    const ok = await confirmDialog({
      title: "Clear test history?", message: "Past questions and answers will be removed. Your documents are not affected.",
      confirmLabel: "Clear history", busyLabel: "Clearing…", danger: true,
      action: () => api.del("/api/knowledge/tests"),
    });
    if (ok) { if (current && current.status !== "processing") resultEl.replaceChildren(); loadHistory(); }
  }

  await docs.reload();
  await loadHistory();
  // Resume a question still being answered (e.g. after navigating away and back).
  try {
    const latest = (await api.get("/api/knowledge/tests")).items[0];
    if (latest && latest.status === "processing") { show(latest); follow(latest.id); }
  } catch { /* history errors are shown above */ }
}

function stepHeader(n, title, text) {
  return h("div", { style: "display:flex;gap:12px;align-items:center" },
    h("span", { class: "step-num step-num-lg" }, String(n)),
    h("div", {}, h("h2", { style: "font-size:15px;font-weight:650" }, title),
      h("p", { style: "color:var(--text-3);font-size:13px;margin-top:2px" }, text)));
}

function stepCard(n, title, text, body) {
  return h("section", { class: "card", style: "margin-top:20px" },
    h("div", { class: "card-head" }, stepHeader(n, title, text)), body);
}

// ------------------------------------------------------------------ result
function flowStates(t) {
  const s = { input: "done", search: "wait", found: "wait", ai: "wait", output: "wait" };
  if (t.status === "processing") {
    if (/Searching/.test(t.stage)) s.search = "active";
    else if (/Generating|Checking/.test(t.stage)) Object.assign(s, { search: "done", found: "done", ai: "active" });
    else Object.assign(s, { search: "done", found: "done", ai: "active" });
    return s;
  }
  if (t.status === "answered") return { input: "done", search: "done", found: "done", ai: "done", output: "done" };
  if (t.status === "no_knowledge") {
    return t.sources.length
      ? { input: "done", search: "done", found: "done", ai: "done", output: "warn" }
      : { input: "done", search: "done", found: "warn", ai: "skip", output: "warn" };
  }
  if (t.status === "ai_unavailable") return { input: "done", search: "done", found: "done", ai: "fail", output: "fail" };
  return { input: "done", search: "fail", found: "wait", ai: "wait", output: "fail" };
}

function flowStrip(t) {
  const states = flowStates(t);
  const label = { done: "Done", active: "In progress", wait: "Waiting", warn: t.status === "no_knowledge" ? "Nothing reliable" : "", skip: "Not needed", fail: "Failed" };
  return h("ol", { class: "flow", "aria-label": "Processing steps" }, FLOW.map(([key, title, ic], i) => h("li", { class: `flow-step flow-${states[key]}` },
    h("span", { class: "flow-dot" }, states[key] === "active" ? h("span", { class: "spinner" }) : icon(states[key] === "done" ? "check" : states[key] === "warn" ? "warning" : states[key] === "fail" ? "close" : ic, 16)),
    h("span", { class: "flow-title" }, title),
    h("span", { class: "flow-state" }, key === "output" && t.status === "no_knowledge" ? "Human review" : label[states[key]]),
    i < FLOW.length - 1 ? h("span", { class: "flow-line", "aria-hidden": "true" }) : null,
  )));
}

function resultView(t, retry, askFollowUp) {
  const processing = t.status === "processing";
  const head = h("div", { class: "card-head" },
    h("div", { style: "display:flex;gap:12px;align-items:center" }, h("span", { class: "step-num step-num-lg" }, "3"),
      h("div", {}, h("h2", {}, "Result"), h("p", { style: "color:var(--text-3);font-size:13px;margin-top:2px" },
        processing ? t.stage || "Working…" : "Input → Knowledge Search → Relevant Information → AI Processing → Output"))),
    statusBadge(t.status));

  const input = h("div", { class: "io-block" }, h("div", { class: "io-label" }, "Input · Your Question"), h("div", { class: "io-question" }, t.question),
    t.search_question && t.search_question !== t.question
      ? h("p", { class: "hint", style: "color:var(--text-3);margin-top:6px" }, "Follow-up understood as: ", h("em", {}, t.search_question))
      : null);
  const followBtn = (t.status === "answered" || t.status === "no_knowledge")
    ? h("button", { type: "button", class: "btn btn-sm", onclick: askFollowUp }, icon("sparkle", 14), "Ask a follow-up")
    : null;

  let knowledge;
  if (processing && !t.sources.length) {
    knowledge = h("div", { class: "io-block" }, h("div", { class: "io-label" }, "Relevant Knowledge"), h("p", { style: "color:var(--text-3)" }, "Searching your documents…"));
  } else if (t.sources.length) {
    knowledge = h("div", { class: "io-block" },
      h("div", { class: "io-label" }, "Relevant Knowledge · information used for this answer"),
      h("div", { class: "stack" }, t.sources.map((s) => h("div", { class: "source-card" },
        h("div", { class: "source-name" }, icon("file", 16), s.document),
        s.excerpts.map((x) => h("div", { class: "excerpt" }, x.page ? h("span", { class: "chip", style: "margin-right:6px" }, `Page ${x.page}`) : null, x.text)),
      ))));
  } else {
    knowledge = h("div", { class: "io-block" },
      h("div", { class: "io-label" }, "Knowledge Match"),
      h("dl", { class: "info-list" },
        h("dt", {}, "Result"), h("dd", {}, h("strong", {}, "No reliable information found.")),
        h("dt", {}, "Recommended Action"), h("dd", {}, h("span", { class: "badge badge-warning" }, "Human Review"))));
  }

  let output;
  if (processing) {
    output = h("div", { class: "io-block" }, h("div", { class: "io-label" }, "Output · AI Answer"),
      h("div", { style: "display:flex;gap:10px;align-items:center;color:var(--text-3)" }, h("span", { class: "spinner" }), t.stage || "Working…"));
  } else if (t.status === "answered") {
    const copy = h("button", { type: "button", class: "btn btn-sm" }, icon("copy", 14), "Copy Answer");
    copy.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(t.answer); notify.success("Answer copied"); }
      catch { notify.warning("Copy failed", "Select the answer text and copy it manually."); }
    });
    output = h("div", { class: "io-block io-output" },
      h("div", { class: "io-label", style: "display:flex;justify-content:space-between;align-items:center;gap:8px" }, "Output · AI Answer", copy),
      h("div", { class: "email-body" }, t.answer),
      h("div", { style: "margin-top:14px" }, h("div", { class: "io-label" }, t.sources.length > 1 ? "Sources" : "Source"),
        h("div", { class: "chips" }, t.sources.map((s) => h("span", { class: "chip" }, icon("file", 14), s.document)))),
      h("div", { style: "margin-top:14px" }, followBtn));
  } else if (t.status === "no_knowledge") {
    output = h("div", { class: "io-block" }, h("div", { class: "io-label" }, "Output"),
      alertBox("warning", "No answer generated",
        h("div", {}, h("p", {}, h("strong", {}, t.answer || "The requested information is not available in the Knowledge Base.")),
          h("p", {}, "Please review this question manually. The AI does not guess when your documents don't contain the answer."))),
      h("div", { style: "margin-top:14px" }, followBtn));
  } else {
    output = h("div", { class: "io-block" }, h("div", { class: "io-label" }, "Output"),
      alertBox("danger", t.status === "ai_unavailable" ? "AI service unavailable" : "Something went wrong",
        t.message || "Please try again.", [h("button", { type: "button", class: "btn btn-sm", onclick: retry }, icon("refresh", 14), "Try again")]));
  }

  return h("section", { class: "card", style: "margin-top:20px" }, head,
    h("div", { class: "card-body" }, flowStrip(t), h("div", { class: "io-grid" }, input, knowledge, output)));
}
