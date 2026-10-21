// Backend API client. Every request goes to this app's own backend; the browser
// never talks to Gmail or the AI service directly and never holds credentials.

export class ApiError extends Error {
  constructor(message, { status = 0, code = "error" } = {}) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

const session = {
  workspace: safeGet(localStorage, "copilot.workspace") || "default",
  adminToken: safeGet(sessionStorage, "copilot.adminToken") || "",
};

function safeGet(store, key) {
  try { return store.getItem(key); } catch { return null; }
}
function safeSet(store, key, value) {
  try { value ? store.setItem(key, value) : store.removeItem(key); } catch { /* storage unavailable */ }
}

export function getWorkspace() { return session.workspace; }
export function setWorkspace(id) { session.workspace = id; safeSet(localStorage, "copilot.workspace", id); }
export function setAdminToken(token) { session.adminToken = token; safeSet(sessionStorage, "copilot.adminToken", token); }
export function hasAdminToken() { return !!session.adminToken; }

function headers(extra = {}) {
  const h = { "X-Requested-With": "copilot", "X-Workspace-Id": session.workspace, ...extra };
  if (session.adminToken) h["X-Admin-Token"] = session.adminToken;
  return h;
}

async function parse(res) {
  let data = null;
  const text = await res.text();
  if (text) {
    try { data = JSON.parse(text); } catch { data = null; }
  }
  if (!res.ok) {
    const err = data && data.error;
    throw new ApiError(
      (err && err.message) || (res.status >= 500 ? "Something went wrong. Please try again." : "The request could not be completed."),
      { status: res.status, code: (err && err.code) || String(res.status) },
    );
  }
  return data;
}

const inflight = new Map();

// After any change, reads that started earlier may hold stale data: never share them again.
async function mutate(method, path, body) {
  try {
    return await request(method, path, body);
  } finally {
    inflight.clear();
  }
}

async function request(method, path, body) {
  const opts = { method, headers: headers(), credentials: "same-origin" };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch {
    throw new ApiError("Unable to reach the server. Check your connection and try again.", { code: "network" });
  }
  return parse(res);
}

export const api = {
  // Identical GETs issued at the same time share one network request.
  get(path) {
    const key = `${session.workspace}|${path}`;
    if (inflight.has(key)) return inflight.get(key);
    const p = request("GET", path).finally(() => inflight.delete(key));
    inflight.set(key, p);
    return p;
  },
  post: (path, body = {}) => mutate("POST", path, body),
  put: (path, body = {}) => mutate("PUT", path, body),
  patch: (path, body = {}) => mutate("PATCH", path, body),
  del: (path) => mutate("DELETE", path),

  // Multipart upload with progress reporting.
  upload(path, formData, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", path);
      Object.entries(headers()).forEach(([k, v]) => xhr.setRequestHeader(k, v));
      xhr.upload.onprogress = (e) => { if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total); };
      xhr.onerror = () => reject(new ApiError("Upload failed. Check your connection and try again.", { code: "network" }));
      xhr.onload = () => {
        inflight.clear();
        let data = null;
        try { data = JSON.parse(xhr.responseText); } catch { data = null; }
        if (xhr.status >= 200 && xhr.status < 300) return resolve(data);
        if (data && (data.uploaded || data.failed)) return resolve(data);
        const err = data && data.error;
        reject(new ApiError((err && err.message) || "Upload failed. Please try again.", { status: xhr.status, code: err && err.code }));
      };
      xhr.send(formData);
    });
  },
};
