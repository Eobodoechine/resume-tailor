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

// ── File download ──────────────────────────────────────────────────────────
// Shared helper for any endpoint that returns binary content (PDF, etc.).
// Handles 401 → redirect exactly like apiFetch, throws RedirectingError so
// callers' catch blocks can bail cleanly with `if (e && e.redirecting) return;`
//
// Root cause of the UUID-filename bug (history):
//   The URL /api/tailor/{UUID}/pdf has no meaningful filename segment.
//   When Chrome can't extract a filename from Content-Disposition (stripped by
//   BaseHTTPMiddleware in the middleware stack), it falls back to URL-path naming:
//   "pdf" has no extension → Chrome walks up to the UUID segment → UUID filename.
//
// The fix (VERSION 7):
//   HEAD first, then a.click() for the real download.
//
//   Key insight: a.download only works for same-origin URLs. Chrome's restriction
//   on downloads without user activation applies to CROSS-origin and blob: URLs.
//   For same-origin URLs, a.click() is allowed without user activation, so we
//   can safely await the HEAD check before firing the click — no ~5s window to
//   worry about.
//
//   Order:
//   1. Await HEAD — fast (~200 ms, DB lookup only, no LibreOffice).
//      Validates auth and record existence before we start anything.
//   2. Fire a.click() only if HEAD passes — GET starts LibreOffice, Chrome
//      downloads the file in the background.
//
//   Why not click-then-HEAD (VERSION 6)?
//   - a.click() starts the 30-60s LibreOffice GET, tying up the Render worker.
//   - HEAD fires concurrently and can hit the per-IP rate limit or get a 503.
//   - User sees "Download failed" even though the download was already triggered.
//
//   Why a.download = suggestedFilename (non-empty)?
//   - Bypasses BaseHTTPMiddleware stripping Content-Disposition entirely.
//   - Chrome uses the attribute value as the filename directly.
//   - Empty string = "derive from URL path" = UUID filename bug.
// VERSION: 7
async function apiDownload(path, suggestedFilename) {
  console.log(`[download] START  path=${path}  filename=${suggestedFilename}`);

  // ── 1. HEAD: auth + record check (fast, no LibreOffice) ──────────────────
  console.log(`[download] firing HEAD ${path}`);
  let res;
  try {
    res = await fetch(API + path, { credentials: "include", method: "HEAD" });
  } catch (networkErr) {
    console.error(`[download] HEAD network error:`, networkErr);
    throw new Error("Download failed — network error. Check your connection.");
  }
  console.log(`[download] HEAD response: status=${res.status} ok=${res.ok}`);
  console.log(`[download] HEAD headers: Content-Disposition="${res.headers.get("Content-Disposition")}" Content-Type="${res.headers.get("Content-Type")}"`);

  if (res.status === 401) {
    console.warn(`[download] 401 — clearing session and redirecting to login`);
    clearToken();
    fetch("/api/auth/session", { method: "DELETE", credentials: "include" }).catch(() => {});
    window.location.href = "/";
    throw new RedirectingError();
  }
  if (!res.ok) {
    console.error(`[download] HEAD failed: ${res.status} ${res.statusText}`);
    throw new Error(`Download failed (${res.status}) — please try again.`);
  }

  // ── 2. Fire download — same-origin + a.download doesn't need user activation ──
  console.log(`[download] HEAD OK — creating anchor and firing click`);
  const a = document.createElement("a");
  a.href = API + path;
  // Non-empty download attribute → Chrome uses this as the filename directly,
  // bypassing Content-Disposition (which BaseHTTPMiddleware strips).
  // IMPORTANT: never set a.download = "" — Chrome falls back to URL-path naming
  // which produces the UUID segment as filename.
  if (suggestedFilename) {
    a.download = suggestedFilename;
  } else {
    console.warn(`[download] no suggestedFilename — Chrome will derive name from URL`);
  }
  a.style.display = "none";
  document.body.appendChild(a);
  // Log exactly what Chrome will use as the filename:
  //   a.download (non-empty, same-origin) → Chrome saves with that exact string
  //   a.download (empty string)           → Chrome derives from URL path → UUID bug
  //   a.download (not set)                → Chrome uses Content-Disposition header
  console.log(`[download] CHROME WILL SAVE AS: "${a.download || "(no download attr — using Content-Disposition)"}"`);
  console.log(`[download] href="${a.href}"  download="${a.download}"`);
  console.log(`[download] a.click() firing now — GET will start LibreOffice on server`);
  a.click();
  setTimeout(() => {
    if (a.parentNode) a.parentNode.removeChild(a);
    console.log(`[download] anchor removed from DOM`);
  }, 100);
  console.log(`[download] DONE — file download initiated in browser`);
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
      // Set the flag BEFORE the async fetch so requireAuth() (which runs
      // synchronously from dashboard's inline <script> right after app.js
      // loads) doesn't redirect to login while the exchange is in flight.
      localStorage.setItem("rt_session_active", "1");

      // Exchange for HttpOnly cookie
      fetch("/api/auth/session", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token })
      })
        .then(res => {
          if (!res.ok) {
            // Exchange failed (e.g. 403 not approved) — clear the flag so
            // the next requireAuth() call on the dashboard will redirect to login.
            localStorage.removeItem("rt_session_active");
          }
          // Clean URL hash regardless of outcome, then redirect to dashboard
          window.location.replace("/dashboard");
        })
        .catch(() => {
          // Network failure — clear flag and let the 401 on first API call handle it
          localStorage.removeItem("rt_session_active");
          window.location.replace("/dashboard");
        });
    }
  }
})();
