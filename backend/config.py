import os
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL       = os.environ["SUPABASE_URL"]
SUPABASE_ANON_KEY  = os.environ["SUPABASE_ANON_KEY"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]  # admin operations
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
ADMIN_EMAIL        = os.environ.get("ADMIN_EMAIL", "enollc21@gmail.com")

CLAUDE_MODEL       = "claude-haiku-4-5-20251001"   # cheapest, fast
RESUME_BUCKET      = "resume-sources"
PDF_BUCKET         = "tailored-pdfs"
