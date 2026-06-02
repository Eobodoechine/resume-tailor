import os
import logging

# Configure root logger so all logger.info/error calls appear in Render logs.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from limiter import limiter
# Importing routes triggers config.py, which raises RuntimeError on any missing
# required env var (see config._require). Boot fails loudly with a clear message
# rather than silently 502-ing on the first AI call.
from routes import auth, profile, resumes, master, tailor, admin
from middleware import SecurityHeadersMiddleware, SlidingSessionMiddleware

# ── Sentry (TD-11) ────────────────────────────────────────────────────────────
# Set SENTRY_DSN in your Render environment variables to enable error tracking.
# If the variable is absent the SDK is a no-op — safe to omit in local dev.
SENTRY_DSN = os.getenv("SENTRY_DSN", "")
if SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.integrations.starlette import StarletteIntegration
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[StarletteIntegration(), FastApiIntegration()],
        traces_sample_rate=0.1,   # 10% of requests traced — adjust as needed
        send_default_pii=False,
    )
    logging.getLogger(__name__).info(
        "[startup] Sentry initialized  dsn_prefix=%s",
        SENTRY_DSN[:20] + "...",
    )
else:
    logging.getLogger(__name__).warning(
        "[startup] SENTRY_DSN not set — Sentry error reporting disabled"
    )

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="Resume Tailor", version="1.0.0")

# Attach rate limiter (TD-06)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Middleware — registered in reverse priority order:
# last add_middleware call runs first in the request chain.
# Order here: SecurityHeaders → SlidingSession → CORS (innermost first)
app.add_middleware(SlidingSessionMiddleware)
app.add_middleware(SecurityHeadersMiddleware)


# CORS — allow_origins=["*"] with allow_credentials=True is invalid per spec.
# Restrict to the production domain and local dev.
# Read allowed origins from env so CORS survives URL changes (TD-04).
# Comma-separated list; falls back to the production URL + local dev.
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.getenv(
        "ALLOWED_ORIGINS",
        "https://resume-tailor-ogop.onrender.com,http://localhost:8000,http://127.0.0.1:8000",
    ).split(",")
    if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register API routes
app.include_router(auth.router)
app.include_router(profile.router)
app.include_router(resumes.router)
app.include_router(master.router)
app.include_router(tailor.router)
app.include_router(admin.router)

# Serve frontend static files
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")

app.mount("/static", StaticFiles(directory=os.path.join(FRONTEND_DIR, "static")), name="static")


@app.get("/")
def serve_index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))

@app.get("/dashboard")
def serve_dashboard():
    return FileResponse(os.path.join(FRONTEND_DIR, "dashboard.html"))

@app.get("/profile")
def serve_profile():
    return FileResponse(os.path.join(FRONTEND_DIR, "profile.html"))

@app.get("/tailor")
def serve_tailor():
    return FileResponse(os.path.join(FRONTEND_DIR, "tailor.html"))

@app.get("/history")
def serve_history():
    return FileResponse(os.path.join(FRONTEND_DIR, "history.html"))

@app.get("/improve")
def serve_improve():
    return FileResponse(os.path.join(FRONTEND_DIR, "improve.html"))

@app.get("/admin")
def serve_admin():
    return FileResponse(os.path.join(FRONTEND_DIR, "admin.html"))

@app.get("/admin-panel")
def serve_admin_legacy():
    """Legacy alias — redirects to /admin."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/admin", status_code=301)

import logging as _logging
_logging.getLogger(__name__).info(
    "RESUME_PDF_ENGINE=%s",
    os.getenv("RESUME_PDF_ENGINE", "libreoffice (default — set to 'playwright' to enable HTML preview)")
)

# TD-12: Health endpoint for uptime pings — prevents Render free-plan cold starts
# when hit by an external cron service (e.g. cron-job.org every 10 minutes).
@app.get("/health")
def health():
    return {"status": "ok"}
