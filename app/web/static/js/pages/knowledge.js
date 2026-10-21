import { api } from "../api.js";
import { icon } from "../icons.js";
import { h, mount, busy } from "../ui.js";
import { pageHead } from "./common.js";
import { documentsPanel } from "../components/documents.js";

export async function render(ctx) {
  const docs = documentsPanel(ctx);
  const searchInput = h("input", { class: "input", type: "search", placeholder: "Search your documents…", "aria-label": "Search knowledge base" });
  const searchBtn = h("button", { type: "submit", class: "btn" }, "Search");
  const results = h("div");
  const form = h("form", { class: "toolbar", role: "search" }, h("div", { class: "search-input grow" }, icon("search", 16), searchInput), searchBtn);

  mount(ctx.el,
    pageHead("Knowledge Base", "Documents the assistant uses for answers and email replies. Shared by the Knowledge Base Test and the Email Assistant.",
      [h("a", { class: "btn", href: "#/knowledge-test" }, icon("sparkle", 16), "Test Knowledge"), docs.uploadButton]),
    docs.el,
    h("section", { class: "card", style: "margin-top:20px" },
      h("div", { class: "card-head" }, h("h2", {}, "Document Search")), form, results),
  );

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const q = searchInput.value.trim();
    if (!q) { results.replaceChildren(); return; }
    busy(searchBtn, "Searching…", async () => {
      try {
        const res = await api.get(`/api/knowledge/search?q=${encodeURIComponent(q)}`);
        if (ctx.alive()) drawResults(res);
      } catch (err) {
        mount(results, h("div", { class: "card-body" }, h("p", { class: "form-error" }, err.message)));
      }
    });
  });

  function drawResults(res) {
    if (!res.total) {
      mount(results, h("div", { class: "card-body", style: "color:var(--text-3)" }, `No relevant information found for “${res.query}”.`));
      return;
    }
    mount(results,
      h("div", { class: "card-body", style: "padding-bottom:0;color:var(--text-2)" },
        h("strong", {}, `${res.total} relevant document${res.total === 1 ? "" : "s"}`), ` for “${res.query}”`),
      h("div", { class: "results" }, res.results.map((r) => h("div", { class: "result" },
        h("div", { class: "result-head" },
          h("div", { style: "display:flex;gap:8px;align-items:center;min-width:0" }, icon("file", 16), h("strong", { class: "truncate" }, r.filename)),
          h("span", { class: "relevance" }, `${Math.round(r.relevance * 100)}% match`)),
        r.matches.map((m) => h("div", { class: "result-match" }, m.page ? h("span", { class: "chip", style: "margin-right:6px" }, `Page ${m.page}`) : null, m.text)),
      ))),
    );
  }

  await docs.reload();
}
