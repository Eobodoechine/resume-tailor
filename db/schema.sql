-- ─────────────────────────────────────────────────────────────────────────────
-- Resume Tailor — Supabase Schema Snapshot
--
-- This file is a point-in-time export of the production schema.
-- It is NOT a migration runner — it documents the current state of all tables,
-- indexes, and RLS policies so the schema can be recreated if needed.
--
-- To apply this on a fresh Supabase project:
--   1. Open the Supabase SQL editor
--   2. Paste and run this file
--   3. Enable Row Level Security on each table (RLS policies are included below)
--
-- Keep this file up to date when adding columns or changing policies.
-- Last updated: 2026-05-26
-- ─────────────────────────────────────────────────────────────────────────────


-- ── Extensions ───────────────────────────────────────────────────────────────
-- uuid_generate_v4() for primary keys
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";


-- ── Table: access_requests ───────────────────────────────────────────────────
-- Users submit email + reason here before being approved by the admin.
-- On approval, a magic link is sent and a Supabase auth user is created.
CREATE TABLE IF NOT EXISTS public.access_requests (
    id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email        TEXT NOT NULL UNIQUE,
    full_name    TEXT,
    reason       TEXT,
    status       TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'approved', 'rejected')),
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    approved_at  TIMESTAMPTZ
);

-- RLS
ALTER TABLE public.access_requests ENABLE ROW LEVEL SECURITY;

-- Admin reads all rows via the service-role key (bypasses RLS) — no policy needed.
-- Public users cannot read or write this table.


-- ── Table: profiles ──────────────────────────────────────────────────────────
-- One row per authenticated user. Created automatically by the
-- handle_new_user trigger on auth.users INSERT, or via the PATCH /api/profile/
-- endpoint for users who pre-date the trigger.
CREATE TABLE IF NOT EXISTS public.profiles (
    id           UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    email        TEXT,
    full_name    TEXT,
    phone        TEXT,
    location     TEXT,
    linkedin_url TEXT,
    website      TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ
);

-- Trigger: auto-create profile row when a new Supabase auth user is created.
CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS TRIGGER LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    INSERT INTO public.profiles (id, email)
    VALUES (NEW.id, NEW.email)
    ON CONFLICT (id) DO NOTHING;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
    AFTER INSERT ON auth.users
    FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();

-- RLS
ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can read their own profile"
    ON public.profiles FOR SELECT
    USING (auth.uid() = id);

CREATE POLICY "Users can update their own profile"
    ON public.profiles FOR UPDATE
    USING (auth.uid() = id);

-- INSERT is handled by the service-role (admin) client — no INSERT policy needed
-- for the user role.


-- ── Table: resume_files ──────────────────────────────────────────────────────
-- Uploaded resume files (PDF/DOCX). Extracted text is stored here so it can
-- be used by the synthesize-master-resume pipeline without re-reading storage.
CREATE TABLE IF NOT EXISTS public.resume_files (
    id             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id        UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    filename       TEXT NOT NULL,
    file_path      TEXT NOT NULL,           -- path in Supabase Storage bucket
    file_type      TEXT,                    -- 'pdf', 'docx', 'doc'
    extracted_text TEXT,                    -- plain text extracted from the file
    uploaded_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS resume_files_user_id_idx ON public.resume_files (user_id);

-- RLS
ALTER TABLE public.resume_files ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can read their own files"
    ON public.resume_files FOR SELECT
    USING (auth.uid() = user_id);

-- INSERT and DELETE are done via the service-role client — no user-role policies needed.


-- ── Table: master_resumes ────────────────────────────────────────────────────
-- One row per user. Synthesized from all uploaded resume_files via Claude.
-- Updated in-place on each re-synthesize or gap-fill session.
CREATE TABLE IF NOT EXISTS public.master_resumes (
    id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id      UUID NOT NULL UNIQUE REFERENCES auth.users(id) ON DELETE CASCADE,
    content      TEXT,                      -- full master resume plain text
    last_updated TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS master_resumes_user_id_idx ON public.master_resumes (user_id);

-- RLS
ALTER TABLE public.master_resumes ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can read their own master resume"
    ON public.master_resumes FOR SELECT
    USING (auth.uid() = user_id);

-- Writes (INSERT/UPDATE) done via service-role client — no user-role write policies needed.


-- ── Table: tailored_resumes ──────────────────────────────────────────────────
-- One row per tailoring run. Stores the job description and the Claude-generated
-- tailored text. Used by the history page and PDF download.
CREATE TABLE IF NOT EXISTS public.tailored_resumes (
    id               UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id          UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    job_title        TEXT,
    company          TEXT,
    job_description  TEXT,
    tailored_content TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS tailored_resumes_user_id_idx     ON public.tailored_resumes (user_id);
CREATE INDEX IF NOT EXISTS tailored_resumes_created_at_idx  ON public.tailored_resumes (user_id, created_at DESC);

-- RLS
ALTER TABLE public.tailored_resumes ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can read their own tailored resumes"
    ON public.tailored_resumes FOR SELECT
    USING (auth.uid() = user_id);

CREATE POLICY "Users can update their own tailored resumes"
    ON public.tailored_resumes FOR UPDATE
    USING (auth.uid() = user_id);

-- INSERT done via service-role client — no user-role INSERT policy needed.


-- ── Storage buckets ──────────────────────────────────────────────────────────
-- Create via Supabase dashboard or CLI — SQL DDL for storage is not supported
-- in the SQL editor. For reference:
--
--   Bucket: resume-sources  (private, 10 MB max)
--     - Stores uploaded PDF/DOCX files
--     - Path: {user_id}/{uuid}/{filename}
--
--   Bucket: tailored-pdfs   (private, 5 MB max)
--     - Reserved for generated PDF caching (not currently used — PDFs are
--       generated on-demand and streamed directly to the browser)
