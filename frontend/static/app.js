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
// Shared helper for binary downloads (PDF, etc.).
// Handles 401 → redirect like apiFetch; throws RedirectingError on failure.
//
// VERSION 8 — URL-path filename strategy
//
// Root cause of UUID-filename bug:
//   BaseHTTPMiddleware (SecurityHeaders + SlidingSession) strips
//   Content-Disposition on binary responses before Chrome sees it.
//   Every approach that relies on the header fails.
//
// Fix: embed the filename as the last URL path segment:
//   /api/tailor/{uuid}/pdf/Company_Role.pdf
// Chrome reads it from the URL directly — no Content-Disposition needed.
// Backend accepts /{record_id}/pdf/{filename}; the URL filename param is
// ignored server-side (always uses DB data). a.download is belt-and-suspenders.
//
// Flow: HEAD first (auth check, no LibreOffice) → a.click() (GET + download).
// Same-origin anchor clicks don't require a user-activation window.
//
async function apiDownload(path, suggestedFilename) {
  console.group(`[download v8] START`);
  console.log(`  path argument    : ${path}`);
  console.log(`  suggestedFilename: ${JSON.stringify(suggestedFilename)}`);

  // ── Build URL with filename as last path segment ──────────────────────────
  // Chrome derives the save-as name from the last URL segment when it carries
  // a file extension.  This bypasses Content-Disposition entirely (which
  // BaseHTTPMiddleware strips on binary responses).
  if (!suggestedFilename) {
    console.warn(`[download v8] ⚠ suggestedFilename is empty/falsy — Chrome will` +
      ` fall back to URL-path naming and may produce a UUID filename!`);
  }
  const fullPath = suggestedFilename
    ? `${path}/${encodeURIComponent(suggestedFilename)}`
    : path;
  console.log(`  fullPath (URL)   : ${fullPath}`);

  // ── 1. HEAD: auth + record-existence check (no LibreOffice) ──────────────
  console.log(`[download v8] 1. HEAD ${fullPath}`);
  let res;
  try {
    res = await fetch(API + fullPath, { credentials: "include", method: "HEAD" });
  } catch (networkErr) {
    console.error(`[download v8] ✗ HEAD network error — check connectivity:`, networkErr);
    console.groupEnd();
    throw new Error("Download failed — network error. Check your connection.");
  }

  // Log every response header we care about so middleware stripping is visible.
  console.log(`[download v8]   HEAD status          : ${res.status} ${res.statusText}`);
  console.log(`[download v8]   Content-Type         : ${res.headers.get("Content-Type")}`);
  console.log(`[download v8]   Content-Disposition  : ${res.headers.get("Content-Disposition")}`);
  if (!res.headers.get("Content-Disposition")) {
    console.warn(`[download v8]   ⚠ Content-Disposition is null — BaseHTTPMiddleware` +
      ` stripped it (expected — URL-path method doesn't need it)`);
  }

  if (res.status === 401) {
    console.warn(`[download v8] ✗ 401 — session expired, redirecting to login`);
    console.groupEnd();
    clearToken();
    fetch("/api/auth/session", { method: "DELETE", credentials: "include" }).catch(() => {});
    window.location.href = "/";
    throw new RedirectingError();
  }
  if (res.status === 404) {
    console.error(`[download v8] ✗ 404 — record not found. Check that the` +
      ` /{record_id}/pdf/{filename} route is deployed on the backend.`);
    console.groupEnd();
    throw new Error(`Download failed (404) — record not found.`);
  }
  if (!res.ok) {
    console.error(`[download v8] ✗ HEAD failed: ${res.status} ${res.statusText}`);
    console.groupEnd();
    throw new Error(`Download failed (${res.status}) — please try again.`);
  }
  console.log(`[download v8]   HEAD OK ✓`);

  // ── 2. Fire download via anchor click ────────────────────────────────────
  // Primary mechanism: Chrome reads the filename from the last URL path segment.
  // Belt-and-suspenders: a.download is also set to the same string.
  const a = document.createElement("a");
  a.href = API + fullPath;
  if (suggestedFilename) {
    a.download = suggestedFilename;
    console.log(`[download v8] 2. anchor.href    = ${a.href}`);
    console.log(`[download v8]    anchor.download = "${a.download}"`);
    console.log(`[download v8]    Chrome will save as: "${suggestedFilename}" (from URL path + download attr)`);
  } else {
    console.warn(`[download v8] 2. anchor.download NOT set — Chrome will derive` +
      ` filename from URL path. If no .pdf extension is in the path, expect UUID filename.`);
  }
  a.style.display = "none";
  document.body.appendChild(a);
  console.log(`[download v8]    a.click() firing — GET will start LibreOffice on server`);
  a.click();
  setTimeout(() => {
    if (a.parentNode) a.parentNode.removeChild(a);
    console.log(`[download v8]    anchor cleaned up`);
  }, 100);
  console.log(`[download v8] DONE — browser download initiated`);
  console.groupEnd();
}

// ── Alert helper ───────────────────────────────────────────────────────────
function showAlert(containerId, message, type = "info") {
  const el = document.getElementById(containerId);
  if (!el) return;
  const div = document.createElement("div");
  div.className = `alert alert-${type}`;
  div.textContent = message;
  el.innerHTML = "";
  el.appendChild(div);
  if (type === "success") setTimeout(() => el.innerHTML = "", 4000);
}

// ── Safe filename helper ───────────────────────────────────────────────────
// Keep word chars, spaces→underscores, hyphens; strip everything else.
// Mirrors backend's _safe_filename_part() regex.
function safeFilePart(s) {
  return (s || "").replace(/[^\w -]/g, "").trim().replace(/\s+/g, "_") || "";
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
