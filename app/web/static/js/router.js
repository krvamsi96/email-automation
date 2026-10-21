// Hash router. Each navigation gets a fresh context; timers and pending results
// of the previous page are discarded so stale responses never overwrite a newer view.

const routes = [];
let current = null;
let leaveGuard = null;

export function route(pattern, loader) {
  const keys = [];
  const regex = new RegExp("^" + pattern.replace(/:(\w+)/g, (_, k) => { keys.push(k); return "([^/]+)"; }) + "$");
  routes.push({ regex, keys, loader });
}

export function navigate(path, { replace = false } = {}) {
  const target = path.startsWith("#") ? path : `#${path}`;
  if (replace) history.replaceState(null, "", target);
  else location.hash = target;
  if (replace) handle();
}

export function currentPath() {
  return (location.hash.slice(1) || "/").split("?")[0];
}

// A page with unsaved changes registers a guard; it is asked before leaving.
export function setLeaveGuard(fn) { leaveGuard = fn; }

let lastHash = location.hash;
async function handle() {
  if (leaveGuard) {
    const allowed = await leaveGuard();
    if (!allowed) {
      history.replaceState(null, "", lastHash || "#/");
      return;
    }
  }
  leaveGuard = null;
  lastHash = location.hash;

  const [path, qs] = (location.hash.slice(1) || "/").split("?");
  const query = Object.fromEntries(new URLSearchParams(qs || ""));
  let match = null;
  for (const r of routes) {
    const m = path.match(r.regex);
    if (m) {
      match = { r, params: Object.fromEntries(r.keys.map((k, i) => [k, decodeURIComponent(m[i + 1])])) };
      break;
    }
  }
  if (!match) return navigate("/", { replace: true });

  if (current) current.dispose();
  const timers = new Set();
  const cleanups = new Set();
  let alive = true;
  const ctx = {
    path, query, params: match.params,
    el: document.getElementById("content"),
    alive: () => alive,
    interval(fn, ms) { const id = setInterval(() => alive && fn(), ms); timers.add(id); return () => { clearInterval(id); timers.delete(id); }; },
    timeout(fn, ms) { const id = setTimeout(() => alive && fn(), ms); timers.add(id); return id; },
    onLeave(fn) { cleanups.add(fn); },
    dispose() {
      alive = false;
      for (const id of timers) { clearInterval(id); clearTimeout(id); }
      for (const fn of cleanups) { try { fn(); } catch (e) { console.error(e); } }
    },
  };
  current = ctx;
  for (const fn of routeListeners) fn(path);
  ctx.el.replaceChildren();
  window.scrollTo(0, 0);
  try {
    const page = await match.r.loader();
    if (ctx.alive()) await page.render(ctx);
  } catch (e) {
    console.error(e);
  }
}

const routeListeners = new Set();
export function onRoute(fn) { routeListeners.add(fn); }

export function start() {
  window.addEventListener("hashchange", handle);
  window.addEventListener("beforeunload", (e) => {
    if (leaveGuard && leaveGuard.dirty && leaveGuard.dirty()) { e.preventDefault(); e.returnValue = ""; }
  });
  handle();
}
