import { appState } from "../store.js";
import { h, mount } from "../ui.js";
import { pageHead } from "./common.js";

export async function render(ctx) {
  const b = appState.info.branding;
  const faq = [
    ["What are the two ways to use the product?", "Knowledge Base Test: upload documents and ask questions to see how the AI answers — no Gmail needed. Email Assistant: connect Gmail so the AI prepares reply drafts for incoming emails."],
    ["Do both use the same knowledge?", "Yes. The Knowledge Base Test and the Email Assistant search the same Knowledge Base, so what you test is what your emails get."],
    ["Why did the AI say it found no information?", "It only answers from your documents. If nothing reliable matches the question, it recommends a human review instead of guessing."],
    ["Does the assistant send emails by itself?", "No. Replies are only ever saved as Gmail drafts. A person reviews, approves and sends each one from Gmail."],
    ["Where do the answers come from?", "From the documents in your Knowledge Base. If no relevant information is found, the assistant drafts a polite acknowledgement (or leaves the email for a person, depending on your AI Preferences)."],
    ["What happens after I approve a draft?", "It stays in Gmail as a draft, marked as approved here. Open Gmail and click Send. The status changes to Sent the next time drafts are refreshed."],
    ["What if I edit a draft?", "Your changes are saved directly to the Gmail draft, and the draft is marked as Edited."],
    ["What happens when I delete a document?", "The file and everything the assistant learned from it are removed. It will no longer be used in replies."],
    ["Why was an email marked for Security Review?", "The email looked like phishing or tried to manipulate the assistant, so no reply was prepared."],
  ];
  mount(ctx.el,
    pageHead("Help", `How ${b.product_name} works.`),
    h("div", { class: "grid grid-2", style: "align-items:start" },
      h("section", { class: "card" },
        h("div", { class: "card-head" }, h("h2", {}, "The workflow")),
        h("div", { class: "card-body" }, h("ol", { class: "help-steps" },
          h("li", {}, h("div", {}, h("strong", {}, "Email arrives. "), "New, unanswered emails are collected from your connected Gmail inbox.")),
          h("li", {}, h("div", {}, h("strong", {}, "AI understands it. "), "Each email is screened for security risks and its question is identified.")),
          h("li", {}, h("div", {}, h("strong", {}, "Knowledge is found. "), "Relevant information is looked up in your Knowledge Base.")),
          h("li", {}, h("div", {}, h("strong", {}, "A reply is drafted. "), "A grounded response is saved as a Gmail draft.")),
          h("li", {}, h("div", {}, h("strong", {}, "You review. "), "Edit, approve or reject it on the Approvals page.")),
          h("li", {}, h("div", {}, h("strong", {}, "You send. "), "Open Gmail and send the approved reply yourself.")),
        )),
      ),
      h("section", { class: "card" },
        h("div", { class: "card-head" }, h("h2", {}, "Frequently asked questions")),
        h("div", { class: "card-body" }, faq.map(([q, a], i) => h("div", { style: i ? "margin-top:16px" : "" },
          h("div", { style: "font-weight:650" }, q), h("p", { style: "color:var(--text-2);margin-top:4px" }, a)))),
        b.support_email ? h("div", { class: "card-foot", style: "justify-content:flex-start" },
          "Need help? Contact ", h("a", { href: `mailto:${b.support_email}` }, b.support_email)) : null,
      ),
    ),
  );
}
