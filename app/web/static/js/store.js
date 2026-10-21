// Small shared state + change notifications.
// After any mutation, pages call `changed(...topics)` so every view that shows
// that data (sidebar counters, dashboard, notification bell) refreshes.

const listeners = new Map();

export const appState = {
  info: null,        // /api/app response (branding, workspace, capabilities)
};

export function on(topic, fn) {
  if (!listeners.has(topic)) listeners.set(topic, new Set());
  listeners.get(topic).add(fn);
  return () => listeners.get(topic).delete(fn);
}

export function changed(...topics) {
  for (const topic of new Set([...topics, "*"])) {
    for (const fn of listeners.get(topic) || []) {
      try { fn(topic); } catch (e) { console.error(e); }
    }
  }
}
