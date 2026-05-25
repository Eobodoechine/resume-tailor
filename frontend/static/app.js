// ─────────────────────────────────────────────
// Shared utilities for Resume Tailor App
// ─────────────────────────────────────────────

const API = "";  // same origin

// ── Token management ──────────────────────────
function getToken() { return localStorage.getItem("rt_token"); }
function setToken(t) { localStorage.setItem("rt_token", t); }
function clearToken() { localStorage.removeItem("rt_token"); }

function authHeaders() {
  return { "Authorization": `Bearer ${getToken()}`, "Content-Type": "application/json" };
}

// Redirect to login if no token
function requireAuth() {
  if (!getToken()) { window.location.href = "/"; }
}

// ── API fetch helpers ──────────────────────────
async function apiFetch(path, options = {}) {
  const res = await fetch(API + path, {
    ...options,
    headers: { ...authHeaders(), ...(options.headers || {}) }
  });
  if (res.status === 401) { clearToken(); window.location.href = "/"; return; }
  const json = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(json.detail || "Request failed");
  return json;
}

async function apiUpload(path, formData) {
  const res = await fetch(API + path, {
    method: "POST",
    headers: { "Authorization": `Bearer ${getToken()}` },
    body: formData
  });
  if (res.status === 401) { clearToken(); window.location.href = "/"; return; }
  const json = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(json.detail || "Upload failed");
  return json;
}

// ── Alert helper ───────────────────────────────
function showAlert(containerId, message, type = "info") {
  const el = document.getElementById(containerId);
  if (!el) return;
  el.innerHTML = `<div class="alert alert-${type}">${message}</div>`;
  if (type === "success") setTimeout(() => el.innerHTML = "", 4000);
}

// ── Format date ────────────────────────────────
function fmtDate(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}

// ── Logout ─────────────────────────────────────
function logout() {
  clearToken();
  window.location.href = "/";
}

// ── Handle Supabase magic link token in URL ────
// After clicking a magic link, Supabase adds #access_token=... to the URL.
// We capture it and store it on page load.
(function handleMagicLinkCallback() {
  const hash = window.location.hash;
  if (hash && hash.includes("access_token=")) {
    const params = new URLSearchParams(hash.replace("#", ""));
    const token = params.get("access_token");
    if (token) {
      setToken(token);
      // Clean URL and redirect to dashboard
      window.location.replace("/dashboard");
    }
  }
})();
