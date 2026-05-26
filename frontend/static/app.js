// ─────────────────────────────────────────────
// Shared utilities for Resume Tailor App
// ─────────────────────────────────────────────

const API = "";  // same origin

// ── Token management ───────────────────────────────────────────────────────
// TD-09: Authentication uses HttpOnly cookies set by POST /api/auth/session.
// The token is NO LONGER stored in localStorage — it lives only in the
// HttpOnly cookie, which is invisible to JavaScript and XSS-safe.
//
// We keep a lightweight session flag in localStorage ("rt_session_active")
// so we can redirect to login when the user is clearly not authenticated
// without exposing the token to JS.
//
// NOTE: We use localStorage (not sessionStorage) so the flag survives tab
// close/reopen. The flag is purely a UX hint — a missing flag means "redirect
// to login" but does NOT mean the server cookie has expired. The real auth
// check always happens server-side on each API call.
//
// Legacy: getToken() returns null (no localStorage token). apiFetch()
// sends credentials: "include" so the browser attaches the cookie automatically.

function getToken() {
  // No longer stored in JS — kept as null stub for backward compat
  return null;
}
function setToken(t) {
  // No-op: token is stored server-side as HttpOnly cookie
  // (kept for backward compat — callers won't break)
}
function clearToken() {
  // Clear the session flag; the actual cookie is cleared via DELETE /api/auth/session
  localStorage.removeItem("rt_session_active");
}

function isLoggedIn() {
  return localStorage.getItem("rt_session_active") === "1";
}

// Redirect to login if session flag is absent
function requireAuth() {
  if (!isLoggedIn()) { window.location.href = "/"; }
}

// ── API fetch helpers ──────────────────────────────────────────────────────
// credentials: "include" ensures the rt_session HttpOnly cookie is sent on
// every request. No Authorization header needed — the cookie handles auth.

// Format a FastAPI/Pydantic error detail for display.
// Pydantic 422 returns `detail` as [{loc, msg, type}, …]; surfacing
// "[object Object]" to users was the old failure mode.
function formatDetail(detail, fallback) {
  if (!detail) return fallback;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map(d => (d && d.msg) ? d.msg : JSON.stringify(d)).join("; ");
  }
  if (typeof detail === "object" && detail.msg) return detail.msg;
  return fallback;
}

// Throwing this means "we already started a navigation away; stop processing".
// Callers' catch blocks should ignore it.
class RedirectingError extends Error {
  constructor() { super("__redirecting"); this.redirecting = true; }
}

async function apiFetch(path, options = {}) {
  const res = await fetch(API + path, {
    ...options,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {})
    }
  });
  if (res.status === 401) {
    clearToken();
    // Also ask the server to clear the cookie (belt + suspenders)
    fetch("/api/auth/session", { method: "DELETE", credentials: "include" }).catch(() => {});
    window.location.href = "/";
    // Throw so callers stop dereferencing the (undefined) result before
    // the navigation actually happens.
    throw new RedirectingError();
  }
  const json = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(formatDetail(json.detail, "Request failed"));
  return json;
}

async function apiUpload(path, formData) {
  const res = await fetch(API + path, {
    method: "POST",
    credentials: "include",
    body: formData
  });
  if (res.status === 401) {
    clearToken();
    fetch("/api/auth/session", { method: "DELETE", credentials: "include" }).catch(() => {});
    window.location.href = "/";
    throw new RedirectingError();
  }
  const json = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(formatDetail(json.detail, "Upload failed"));
  return json;
}

// ── Blob / file download ───────────────────────────────────────────────────
// Shared helper for any endpoint that returns binary content (PDF, etc.).
// Handles 401 → redirect exactly like apiFetch, throws RedirectingError so
// callers' catch blocks can bail cleanly with `if (e && e.redirecting) return;`
async function apiDownload(path, filename) {
  const res = await fetch(API + path, { credentials: "include" });
  if (res.status === 401) {
    clearToken();
    fetch("/api/auth/session", { method: "DELETE", credentials: "include" }).catch(() => {});
    window.location.href = "/";
    throw new RedirectingError();
  }
  if (!res.ok) throw new Error("Download failed — please try again.");

  const blob = await res.blob();
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement("a");
  a.href          = url;
  a.download      = filename;
  a.style.display = "none";
  document.body.appendChild(a);
  a.click();
  setTimeout(() => {
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }, 1500);
}

// ── Alert helper ───────────────────────────────────────────────────────────
function showAlert(containerId, message, type = "info") {
  const el = document.getElementById(containerId);
  if (!el) return;
  el.innerHTML = `<div class="alert alert-${type}">${message}</div>`;
  if (type === "success") setTimeout(() => el.innerHTML = "", 4000);
}

// ── Format date ────────────────────────────────────────────────────────────
function fmtDate(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}

// ── Logout ─────────────────────────────────────────────────────────────────
async function logout() {
  try {
    // Ask server to clear the HttpOnly cookie
    await fetch("/api/auth/session", { method: "DELETE", credentials: "include" });
  } catch (_) {}
  clearToken();
  window.location.href = "/";
}

// ── Handle Supabase magic link token in URL ────────────────────────────────
// After clicking a magic link, Supabase redirects to /dashboard#access_token=…
// We extract the token, POST it to /api/auth/session to set an HttpOnly cookie,
// then clean up the URL fragment. The token never touches localStorage.
(function handleMagicLinkCallback() {
  const hash = window.location.hash;
  if (hash && hash.includes("access_token=")) {
    const params = new URLSearchParams(hash.replace("#", ""));
    const token = params.get("access_token");
    if (token) {
      // Exchange for HttpOnly cookie
      fetch("/api/auth/session", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token })
      })
        .then(res => {
          if (res.ok) {
            localStorage.setItem("rt_session_active", "1");
          }
          // Clean URL hash regardless of outcome, then redirect to dashboard
          window.location.replace("/dashboard");
        })
        .catch(() => {
          // Exchange failed — fall back to dashboard anyway
          // (user will get 401 on first API call and be redirected to login)
          window.location.replace("/dashboard");
        });
    }
  }
})();
