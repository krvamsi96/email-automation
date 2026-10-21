// Helpers shared by several pages.
import { api } from "../api.js";
import { icon } from "../icons.js";
import { changed } from "../store.js";
import { h, mount, busy, notify } from "../ui.js";

export function pageHead(title, subtitle, actions = []) {
  return h("div", { class: "page-head" },
    h("div", {}, h("h1", {}, title), subtitle ? h("p", {}, subtitle) : null),
    actions.length ? h("div", { class: "page-actions" }, actions) : null,
  );
}

// "Check for new emails" button + live progress banner, shared by Dashboard and Emails.
export function checkEmailsControl(ctx, { onFinished, hasAccounts }) {
  const banner = h("div", { hidden: true });
  const button = h("button", { type: "button", class: "btn btn-primary", disabled: !hasAccounts,
    title: hasAccounts ? null : "Connect an email account first" }, icon("refresh", 16), "Check for new emails");
  let stopPoll = null;

  function showJob(job) {
    if (!job || job.status !== "running") {
      banner.hidden = true;
      button.disabled = !hasAccounts;
      return;
    }
    button.disabled = true;
    banner.hidden = false;
    mount(banner, h("div", { class: "job", role: "status" },
      h("div", { class: "spinner" }),
      h("div", { class: "job-text" },
        h("strong", {}, job.stage || "Processing…"),
        h("span", {}, job.found ? `${job.processed} of ${job.found} email(s) processed` : "Looking for unanswered emails…"),
      ),
    ));
  }

  function poll() {
    if (stopPoll) return;
    stopPoll = ctx.interval(async () => {
      let res;
      try { res = await api.get("/api/emails/check"); } catch { return; }
      showJob(res.job);
      if (res.job && res.job.status !== "running") {
        stopPoll(); stopPoll = null;
        reportJob(res.job);
        changed("emails", "drafts", "dashboard");
        if (onFinished) onFinished();
      }
    }, 1500);
  }

  button.addEventListener("click", () => busy(button, "Starting…", async () => {
    try {
      const job = await api.post("/api/emails/check");
      showJob(job);
      poll();
    } catch (e) {
      if (e.code === "job_running") { poll(); return; }
      notify.error("Could not check emails", e.message);
    }
  }).then(() => { if (stopPoll) button.disabled = true; }));

  return {
    button, banner,
    resume(job) { showJob(job); if (job && job.status === "running") poll(); },
  };
}

function reportJob(job) {
  if (job.status === "failed") {
    notify.error("Checking emails failed", job.message);
    return;
  }
  if (job.errors && job.errors.length) notify.warning("Some accounts could not be checked", job.errors.join(" "));
  if (job.found) {
    notify.success("Emails processed",
      `${job.found} new email(s). ${job.drafts_created} AI draft(s) are ready for review.`);
  } else {
    notify.info("You're up to date", "No new emails to process.");
  }
}

export function sourcesChips(sources) {
  if (!sources || !sources.length) return null;
  return h("div", { class: "chips" }, sources.map((s) => h("span", { class: "chip" }, icon("file", 14), s)));
}
