"""
Configuration loaded from environment variables.

Required vars raise a clear, actionable RuntimeError on import if missing —
no more cryptic KeyError on first AI call. Missing keys appear in Render
deploy logs as a single descriptive line, not a stack trace.
"""
import os
from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name, "")
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            "Set it in Render → Environment, or in your local .env file."
        )
    return value


SUPABASE_URL       = _require("SUPABASE_URL")
SUPABASE_ANON_KEY  = _require("SUPABASE_ANON_KEY")
SUPABASE_SERVICE_KEY = _require("SUPABASE_SERVICE_KEY")  # admin operations
ANTHROPIC_API_KEY  = _require("ANTHROPIC_API_KEY")
RESEND_API_KEY     = os.getenv("RESEND_API_KEY", "")   # optional: enables Resend email delivery
ADMIN_EMAIL        = os.environ.get("ADMIN_EMAIL", "enollc21@gmail.com")

CLAUDE_MODEL       = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
RESUME_BUCKET      = os.getenv("RESUME_BUCKET", "resume-sources")
PDF_BUCKET         = os.getenv("PDF_BUCKET", "tailored-pdfs")

# Cookie settings — shared by auth routes and the sliding-session middleware.
# Set ENV=development in your local .env to allow plain-http cookies in dev.
COOKIE_SECURE  = os.getenv("ENV", "production") == "production"
COOKIE_MAX_AGE = 60 * 60 * 24 * 7   # 7 days
