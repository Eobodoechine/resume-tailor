# Resume Tailor — Codebase Guide

Written for Nnamdi. Every file explained: what it does, why it exists, and what breaks if you remove it.

---

## What the app does in one sentence

A user uploads their resume files, the app synthesizes them into one master resume, and then generates a tailored version for any job description — streamed live, with an AI coach that helps refine it, and a PDF download at the end.

---

## Top-level layout

```
resume-tailor-app/
├── backend/          ← Python FastAPI server (the brain)
├── frontend/         ← HTML/CSS/JS pages (what users see)
├── requirements.txt  ← Python packages needed to run on Render
└── requirements-test.txt  ← extra packages for running tests locally
```

---

## Backend

The backend is a Python web server built with FastAPI. It handles everything:
user authentication, file uploads, calling Claude AI, generating PDFs, and
storing data in Supabase.

### `backend/main.py` — The front door

This is the entry point. It does four things:

1. **Creates the FastAPI app** — the object that receives every HTTP request.
2. **Attaches middleware** — CORS (controls which domains can call the API), and Sentry error tracking.
3. **Registers all the API routes** — imports each route file and plugs them in.
4. **Serves the frontend files** — maps URLs like `/dashboard` to HTML files on disk.

If you remove this file, the server can't start. If you remove a route registration here, that entire section of the app stops working (e.g., removing `app.include_router(tailor.router)` breaks resume generation entirely).

---

### `backend/config.py` — Environment variables

Loads sensitive values from the `.env` file (or Render's environment variables in production) and exposes them as Python constants that the rest of the app imports.

Variables it loads:
- `SUPABASE_URL` — the address of the database
- `SUPABASE_ANON_KEY` — the public key (limited database access)
- `SUPABASE_SERVICE_KEY` — the admin key (full database access — keep secret)
- `ANTHROPIC_API_KEY` — your Claude AI key
- `ADMIN_EMAIL` — your email, which bypasses the access request approval check
- `CLAUDE_MODEL` — which Claude model to use (defaults to Haiku, changeable via env var without code edits)
- `RESUME_BUCKET` — the Supabase Storage bucket name where uploaded files live

If this file can't find a required variable (e.g., SUPABASE_URL is missing), the server crashes on startup with a `KeyError`. That's intentional — you shouldn't run the app without a database.

---

### `backend/limiter.py` — Rate limiting

Controls how many requests a single IP address can make per minute. Prevents abuse — without this, anyone could hammer the AI endpoints and run up your Anthropic bill or take the server down.

Key detail: uses `X-Forwarded-For` header instead of the raw connection IP. This matters because on Render, all traffic arrives through a proxy — every request looks like it comes from the same internal IP without this fix, making the rate limit useless.

---

### `backend/dependencies/auth.py` — Shared authentication

Every protected endpoint needs to verify the user is logged in. Rather than copy that logic into every route file (which was the original approach and was fragile), this file provides one reusable `require_user` function.

How it works:
1. Checks for a session cookie (`rt_session`) set when the user logged in via magic link.
2. Falls back to an `Authorization: Bearer` header (for compatibility).
3. If neither is present, immediately returns 401 — the route handler never runs.
4. Verifies the token with Supabase and returns an `AuthContext` object containing the user and their token.

Routes use it like this: `ctx: AuthContext = Depends(require_user)`. FastAPI calls `require_user` automatically before the route handler runs. The route then uses `ctx.user.id` to know whose data to touch.

If you remove this file, every protected route breaks immediately.

---

### `backend/routes/` — The API endpoints

Each file handles a specific feature area. These are what the frontend calls.

#### `routes/auth.py` — Login, logout, access control

Handles the entire auth flow:

- `POST /api/auth/request-access` — A new user submits their email to get approved. Creates a "pending" record in the `access_requests` Supabase table. You (as admin) then change that status to "approved" in the Supabase dashboard.
- `POST /api/auth/login` — An approved user requests a magic link. Checks the `access_requests` table, then calls Supabase to email them a one-click login link.
- `POST /api/auth/session` — After the user clicks the magic link, their browser lands on `/dashboard` with a token in the URL. This endpoint exchanges that token for a secure HttpOnly cookie so JavaScript can't steal it. Also re-checks approval status.
- `DELETE /api/auth/session` — Logout. Clears the cookie.

Why magic links instead of passwords? No passwords to steal, no reset emails to build, no bcrypt hashing to manage.

#### `routes/resumes.py` — File upload and management

Handles the first step: getting the user's existing resume files into the system.

- `GET /api/resumes/` — Lists all uploaded files for the user.
- `POST /api/resumes/upload` — Accepts a PDF or DOCX file. Validates the extension, MIME type, and file size (10 MB max, 100 files max per user). Then checks the actual file bytes ("magic bytes") to confirm the file really is what it claims — a renamed `.exe` with a `.pdf` extension is rejected. Extracts text from the file and saves everything to Supabase.
- `DELETE /api/resumes/{file_id}` — Deletes a file from both Supabase Storage and the database. Verifies ownership first.

#### `routes/master.py` — Master resume synthesis

After uploading files, the user triggers synthesis.

- `POST /api/master/synthesize` — Pulls all uploaded resume texts for the user, combines them into one prompt, sends it to Claude, and saves the resulting "master resume" in the `master_resumes` table. This becomes the source of truth for all tailoring.
- `GET /api/master/` — Returns the current master resume content so the dashboard can display it.

#### `routes/tailor.py` — The core product

This is the most complex file. It handles everything that happens on the Tailor page.

- `POST /api/tailor/fetch-jd` — Given a job posting URL, fetches the page HTML and extracts the job description text. First checks that the URL is safe (blocks localhost, private IPs, non-http schemes — prevents attackers from using the server to probe internal infrastructure). Then tries to extract structured data from the page's JSON-LD metadata (works even on JavaScript-heavy ATS platforms like Greenhouse). Falls back to stripping all HTML tags.

- `POST /api/tailor/` — The blocking version of resume tailoring. Sends master resume + job description to Claude, waits for the full response (can take 15-30 seconds), then returns it. Kept for compatibility but the streaming version is preferred.

- `POST /api/tailor/stream` — The streaming version. Instead of waiting 30 seconds for a complete response, this endpoint sends text back to the browser word-by-word as Claude generates it. Uses Server-Sent Events (SSE) format — the browser receives a stream of `data: {"chunk": "..."}` lines and displays them in real time. After streaming completes, saves the full text to Supabase and sends a final `data: {"done": true, "id": "..."}` event so the frontend knows the record ID.

- `GET /api/tailor/history` — Returns all past tailored resumes for the user (id, job title, company, date).

- `POST /api/tailor/{record_id}/refine` — The AI coaching chat. Given the tailored resume and conversation history, Claude asks targeted questions to surface better metrics and achievements, then rewrites the resume when the user provides new information. If Claude produces an updated resume in its response (wrapped in `UPDATE_TAILORED_RESUME: ... END_UPDATE` markers), the route saves it to the database automatically.

- `GET /api/tailor/{record_id}/pdf` — Generates and returns a PDF of the tailored resume. Parses the saved plain text into `ResumeData` via `resume_parser`, renders it through the `FDEDocxRenderer` (DOCX → LibreOffice → PDF), then sends back the bytes with a `Content-Disposition: attachment` header so the browser downloads it.

#### `routes/profile.py` — User profile

- `GET /api/profile/` — Returns the user's profile (name, email, phone, LinkedIn URL, etc.). If no row exists yet (rare — the `handle_new_user` trigger normally creates one on signup) it returns an empty-shape object so the dashboard's parallel fetch doesn't reject.
- `PATCH /api/profile/` — Upserts the profile. Filters out `None` and `""` so leaving a field blank in the form doesn't wipe previously-saved data. This data appears in the resume header — if your name or LinkedIn URL changes, update it here.

#### `routes/admin.py` — Admin panel

Routes only accessible to the admin email (`ADMIN_EMAIL`) or any user with `profiles.is_admin = TRUE`. Uses the same cookie/Bearer auth as every other protected route via the shared `require_user` dependency.

- `GET /api/admin/requests?status=pending|approved|rejected|all` — Lists access requests filtered by status.
- `POST /api/admin/approve` — Body `{request_id}`. Approves the request, sends a Supabase magic-link invite, and marks `reviewed_at`.
- `POST /api/admin/reject` — Body `{request_id}`. Marks the request rejected.
- `GET /api/admin/users` — Lists profile rows (id, email, full_name, created_at).

---

### `backend/services/` — Business logic

These files don't handle HTTP — they contain the actual work: talking to Claude, generating PDFs, extracting text.

#### `services/claude.py` — All AI calls

Three functions:

- `synthesize_master_resume(resume_texts, profile)` — Takes all your raw resume texts and produces one comprehensive master resume. The prompt instructs Claude to keep everything, remove duplicates, and use a specific pipe-separated format for role headers (`Job Title | Company | Dates`) that the PDF generator can parse.

- `tailor_resume(master_resume, job_description, profile, ...)` — Takes the master resume and a job description, returns a tailored version. Blocking — waits for the complete response.

- `stream_tailor_resume(master_resume, job_description, profile, ...)` — Same as `tailor_resume` but a generator that yields text chunks. Used by the streaming endpoint.

Both tailoring functions use the same prompt via `_build_tailor_prompt()` — this is important because if you update the prompt format (e.g., change the bullet character or section headers), you only need to change one place.

#### `services/resume_parser.py` — Plain text → ResumeData

Converts the structured plain-text resume Claude produces into a `ResumeData` TypedDict that any renderer can consume. The parse logic mirrors the format contract enforced by the Claude prompt in `claude.py`:

- Splits text into named sections (SUMMARY, EXPERIENCE, SKILLS, EDUCATION, CERTIFICATIONS)
- Parses EXPERIENCE into `ExperienceRole` dicts (`title`, `company`, `dates`, `bullets`)
- Parses SKILLS lines (`Category: item1, item2`) into `SkillGroup` dicts with `items` as a proper list
- Parses EDUCATION and CERTIFICATIONS into typed entries
- Contact fields (`name`, `email`, `phone`, `linkedin`) come from the user's profile, not the text

This is the bridge between Claude's text output and the renderer layer.

#### `backend/renderers/` — Template renderers

Template-agnostic PDF generation system. Each renderer takes a `ResumeData` dict and returns PDF bytes.

- **`renderers/base.py`** — `ResumeData` TypedDict (the shared data contract) and the `Renderer` Protocol every renderer must implement.
- **`renderers/fde_docx.py`** — The FDE branded renderer. Loads the actual `fde_template.docx` (pixel-perfect two-column format), surgically replaces content by cloning styled paragraph XML nodes via `lxml deepcopy`, then converts to PDF using LibreOffice headless (`libreoffice --headless --convert-to pdf`). This preserves 100% of the visual styling — fonts, colors, spacing, bullet symbols — with zero approximation.
- **`renderers/registry.py`** — Maps template IDs to renderer classes. `get_renderer("fde_docx")` returns a ready-to-use renderer. Future templates (image overlay, plain DOCX, HTML/CSS) get added here with one line.

Why DOCX→LibreOffice instead of HTML→WeasyPrint? DOCX lets you design the template in Word exactly as it will appear. WeasyPrint approximated styles and could never match the branded layout precisely.

#### `_archive/pdf_generator.py` — **ARCHIVED**

The original WeasyPrint HTML-based PDF renderer. Moved to `backend/_archive/` — no longer called by any route. Superseded by `renderers/fde_docx.py`. Safe to delete permanently once the new renderer is confirmed stable in production.

#### `services/extractor.py` — Text extraction from uploaded files

Given raw file bytes and a filename, returns the text content:
- For PDFs: uses `pdfplumber` to extract text from each page.
- For DOCX files: uses `python-docx` to extract text from paragraphs.
- For DOC files (legacy Word format): best-effort text extraction.

This text becomes the "extracted_text" stored alongside the file metadata, and is what gets fed to Claude for synthesis.

#### `services/supabase_client.py` — Database connections

Three functions:

- `get_admin_client()` — Returns a Supabase client using the service role key. Bypasses Row Level Security (RLS). Used for admin operations like inserting records on behalf of a user, or operations where the service needs to write data that RLS INSERT policies might block.

- `get_client(user_token)` — Returns a Supabase client that operates as the logged-in user. Respects RLS policies — a user can only see their own rows. Used for reading user-owned data (resumes, tailored history, profile).

- `get_user_from_token(token)` — Validates a Supabase JWT and returns the user object. Used by the auth dependency.

The distinction between admin and user client matters for security: using the admin client for reads would mean one user could accidentally access another user's data if there's a bug in the user_id filtering code. The user client uses Supabase's RLS as a hard enforcement layer.

---

### `backend/tests/` — Automated test suite

Running `pytest tests/` verifies the app works without touching real databases or AI APIs.

#### `conftest.py` — Test setup and mocks

Runs before every test. Replaces heavy external libraries (Supabase, Anthropic, WeasyPrint) with fake objects (MagicMocks) so tests run instantly without real credentials. Also makes the rate limiter a no-op so tests don't need to worry about rate limits.

Key insight: it stubs external libraries but NOT internal pure-logic services (like `pdf_generator.py`), so those are tested with real logic.

#### `test_auth_dependency.py` — Auth logic tests (46 → split across files)

Tests the `require_user` dependency directly: missing header returns 401, invalid token returns 401, valid cookie takes priority over header, etc.

#### `test_file_upload.py` — File validation tests

Tests magic byte validation and the `_parse_experience` parser directly. Confirms that a `.pdf` file containing non-PDF bytes is rejected, DOCX files are validated correctly, and the experience parser correctly splits role headers from bullet points.

#### `test_jd_fetch.py` — JD URL security tests

Tests the SSRF protection and HTML extraction: localhost URLs are blocked, `file://` scheme is blocked, private IP ranges are blocked, JSON-LD extraction works for arrays (where the JobPosting isn't the first item), HTML stripping removes scripts and nav.

#### `test_timestamps.py` — Prompt format contract tests

Confirms that the Claude prompts always request the `|` pipe format for role headers, and that timestamp handling uses timezone-aware datetimes.

#### `test_http_layer.py` — HTTP stack tests (new)

Tests the full FastAPI HTTP layer using TestClient — real HTTP requests to real routes. Catches routing issues, Pydantic validation rejections, response shape mismatches, and auth wiring that pure unit tests can't see.

Key things only this file tests:
- Sending `role: "system"` in the refine history → 422 before Claude is ever called
- A JD too long → 422 from Pydantic before the route handler runs
- No auth header → 401 for every protected endpoint
- Streaming endpoint returns `Content-Type: text/event-stream`
- SSE events are correctly framed as `data: {...}\n\n`
- `done` event with null `id` (Supabase insert failure) is handled gracefully

#### `tests/integration/` — Real database tests (new)

These tests connect to a real Supabase project. They are **skipped automatically** when the `TEST_SUPABASE_URL` environment variable is not set — so the main test suite always runs cleanly without credentials.

When you DO run them (export `TEST_SUPABASE_URL`, `TEST_SUPABASE_ANON_KEY`, `TEST_SUPABASE_SERVICE_KEY`, then `pytest tests/integration/`), they validate:
- All expected tables exist in the schema
- All expected column names are spelled correctly
- Insert/read/delete round-trips work
- The `access_requests` status workflow functions correctly

**Why this matters:** The unit tests mock Supabase entirely. If you renamed the `tailored_content` column to `content` in a Supabase migration, every unit test would still pass — but the app would silently return null for every tailored resume. The integration tests catch that class of bug.

To run integration tests against your real Supabase project:
```bash
export TEST_SUPABASE_URL=https://your-project.supabase.co
export TEST_SUPABASE_ANON_KEY=eyJ...
export TEST_SUPABASE_SERVICE_KEY=eyJ...
pytest tests/integration/ -v
```

---

## Frontend

Static HTML/CSS/JS files. The backend serves them — there's no separate frontend build process.

### `frontend/static/app.js` — Shared utilities

Included on every page. Provides:
- `apiFetch(path, options)` — wrapper around `fetch()` that automatically includes the session cookie (`credentials: "include"`), handles 401 responses (redirects to login), and parses JSON.
- `logout()` — calls the logout endpoint, clears the session flag, redirects to login.
- `isLoggedIn()` / `requireAuth()` — checks `localStorage` for a session flag. If absent, redirects to login. (The real auth check always happens server-side — this is just a UX shortcut to avoid a round-trip before showing the page.)
- Magic link handler — runs on every page load. If the URL contains `#access_token=...` (set by Supabase after a magic link click), it extracts the token, POSTs it to `/api/auth/session` to exchange it for a secure cookie, sets the `localStorage` flag, and redirects to the dashboard.

### `frontend/static/style.css` — Styles

All CSS for the app. Uses CSS variables for colors (`--navy`, `--green`, `--muted`, etc.) so the palette is defined in one place.

### `frontend/index.html` — Login page

The first page users see. Two sections:
1. Existing approved users: enter email → magic link sent.
2. New users: fill in name, email, reason → access request submitted.

### `frontend/dashboard.html` — Main dashboard

After login, users land here. Shows:
- Upload area for resume files (drag and drop or click to select).
- List of uploaded files with delete buttons.
- "Synthesize Master Resume" button that triggers `POST /api/master/synthesize`.
- The synthesized master resume text (editable for minor corrections).
- Navigation to the Tailor page and History.

### `frontend/tailor.html` — The main product page

The most complex frontend file. A 4-step flow:

1. **Step 1 — Job input:** Paste a job description OR enter a URL (which triggers `POST /api/tailor/fetch-jd` to extract the text). Enter job title and company.
2. **Step 2 — Generating:** Calls `POST /api/tailor/stream`. Displays text progressively as Claude generates it via the SSE stream.
3. **Step 3 — Refine:** Shows the generated resume alongside an AI coaching chat panel. Each message calls `POST /api/tailor/{id}/refine`. If Claude returns an updated resume, it replaces the displayed version in real time.
4. **Step 4 — Download:** Button calls `GET /api/tailor/{id}/pdf` and triggers a browser download.

### `frontend/history.html` — Past resumes

Shows all tailored resumes the user has generated (`GET /api/tailor/history`), with links to re-open the refine chat or download the PDF for each.

### `frontend/profile.html` — User profile

Form to update name, email, phone, LinkedIn URL, website, location. This data appears in the resume header on every generated resume.

### `frontend/admin.html` — Admin panel

Only accessible at `/admin-panel`. Shows pending access requests from people who have requested access. Approve or reject buttons call the admin API routes.

---

## Data flow for the core use case

Here's what happens from end to end when a user generates a tailored resume:

```
User clicks "Generate" on tailor.html
  ↓
Frontend: POST /api/tailor/stream {job_description, job_title, company}
  ↓
Backend: require_user() verifies cookie → AuthContext
  ↓
Backend: get_client(token).table("master_resumes").select() → fetches master resume
  ↓
Backend: get_client(token).table("profiles").select() → fetches profile (name, contact info)
  ↓
Backend: streams from claude_service.stream_tailor_resume(master, jd, profile)
  ↓
Claude API: generates tailored resume token by token
  ↓
Backend: yields "data: {'chunk': '...'}\n\n" for each token
  ↓
Frontend: EventSource/ReadableStream reader appends each chunk to the display
  ↓  (user sees resume appearing word by word)
  ↓
Stream ends: backend inserts full text to tailored_resumes table
  ↓
Backend: yields "data: {'done': true, 'id': 'record-uuid'}\n\n"
  ↓
Frontend: stores record ID, shows AI coaching panel
  ↓
User types refinement message → POST /api/tailor/{id}/refine
  ↓
Backend: Claude reads current resume + JD + conversation → responds with coaching question or rewrite
  ↓
User clicks Download → GET /api/tailor/{id}/pdf
  ↓
Backend: resume_parser.text_to_resume_data(tailored_text, profile) → ResumeData TypedDict
  ↓
Backend: FDEDocxRenderer.render(data) — loads fde_template.docx, clones styled XML nodes
         via lxml deepcopy (title, dates, bullets per role; skills, certs, education in sidebar)
  ↓
Backend: LibreOffice headless --convert-to pdf → PDF bytes
  ↓
Browser: downloads PDF file
```

---

## Key design decisions

**Magic links instead of passwords** — No password storage, no reset flows, no bcrypt. The tradeoff is that login requires email access (which is fine for a low-volume app with known users).

**Gated access (request + approval)** — You explicitly approve each user. Prevents unauthorized API usage and Claude cost bleed.

**Streaming over blocking** — The blocking `/api/tailor/` endpoint takes 15-30 seconds with no feedback. Streaming makes the app feel instant — users see output in under 1 second and watch it build.

**Plain text as the resume format** — Claude generates structured plain text (not JSON, not markdown). The PDF generator then parses that text. This is simpler than asking Claude to generate JSON and more flexible than markdown, but it does create a contract: Claude's output format must match what the PDF parser expects. The pipe separator (`|`) for role headers is the key contract point.

**HttpOnly cookie for auth** — After much iteration, the JWT lives in an HttpOnly cookie invisible to JavaScript. This prevents XSS attacks from stealing the session token. The `sessionStorage`/`localStorage` flag is just a UX hint to skip the redirect — the real auth happens server-side on every request.

**Supabase RLS** — Row Level Security policies on the database mean that even if there's a bug in the Python code (e.g., forgetting to filter by `user_id`), a user client can only access their own rows. The service/admin client bypasses this, so it's used only for writes where RLS might interfere, not for reads of user data.
