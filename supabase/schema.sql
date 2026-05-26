-- ============================================================
-- Resume Tailor App — Supabase Schema
-- Run this in your Supabase SQL Editor (project > SQL Editor)
-- ============================================================

-- Profiles (extends auth.users)
CREATE TABLE public.profiles (
  id          UUID REFERENCES auth.users(id) ON DELETE CASCADE PRIMARY KEY,
  email       TEXT NOT NULL,
  full_name   TEXT,
  phone       TEXT,
  location    TEXT,
  linkedin_url TEXT,
  website     TEXT,
  is_admin    BOOLEAN DEFAULT FALSE,
  created_at  TIMESTAMPTZ DEFAULT NOW(),
  updated_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Access requests (before admin approval)
CREATE TABLE public.access_requests (
  id           UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  email        TEXT NOT NULL UNIQUE,
  full_name    TEXT,
  reason       TEXT,
  status       TEXT DEFAULT 'pending',   -- pending | approved | rejected
  requested_at TIMESTAMPTZ DEFAULT NOW(),
  reviewed_at  TIMESTAMPTZ
);

-- Uploaded resume source files
CREATE TABLE public.resume_files (
  id             UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id        UUID REFERENCES auth.users(id) ON DELETE CASCADE,
  filename       TEXT NOT NULL,
  file_path      TEXT NOT NULL,   -- path inside Supabase storage bucket
  file_type      TEXT,            -- pdf | docx
  extracted_text TEXT,
  uploaded_at    TIMESTAMPTZ DEFAULT NOW()
);

-- Master resume (synthesized from all uploaded files)
CREATE TABLE public.master_resumes (
  id           UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id      UUID REFERENCES auth.users(id) ON DELETE CASCADE UNIQUE,
  content      TEXT NOT NULL,
  last_updated TIMESTAMPTZ DEFAULT NOW()
);

-- Tailored resume history
CREATE TABLE public.tailored_resumes (
  id               UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id          UUID REFERENCES auth.users(id) ON DELETE CASCADE,
  job_title        TEXT,
  company          TEXT,
  job_description  TEXT NOT NULL,
  tailored_content TEXT NOT NULL,
  pdf_path         TEXT,          -- path inside Supabase storage bucket (nullable)
  created_at       TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- Row Level Security
-- ============================================================

ALTER TABLE public.profiles         ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.resume_files     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.master_resumes   ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tailored_resumes ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.access_requests  ENABLE ROW LEVEL SECURITY;

-- Profiles: users see only their own
CREATE POLICY "Own profile" ON public.profiles
  FOR ALL USING (auth.uid() = id);

-- Resume files: users see only their own
CREATE POLICY "Own resume files" ON public.resume_files
  FOR ALL USING (auth.uid() = user_id);

-- Master resume: users see only their own
CREATE POLICY "Own master resume" ON public.master_resumes
  FOR ALL USING (auth.uid() = user_id);

-- Tailored resumes: users see only their own
CREATE POLICY "Own tailored resumes" ON public.tailored_resumes
  FOR ALL USING (auth.uid() = user_id);

-- Access requests:
--   INSERT — anyone (anon role) may submit a request; the row is harmless
--            until an admin approves it.
--   SELECT — intentionally NO policy. With RLS enabled and no SELECT policy,
--            PostgREST returns nothing to anon/authenticated callers. The
--            admin panel reads this table via the service-role key (which
--            bypasses RLS) in backend/routes/admin.py.
--   UPDATE/DELETE — same: admin-only via the service-role key.
-- Don't add a "users can read their own request" policy without also
-- considering whether that leaks pending/rejected status to anyone who
-- guesses an email.
CREATE POLICY "Public insert access request" ON public.access_requests
  FOR INSERT WITH CHECK (TRUE);

-- ============================================================
-- Storage Buckets
-- Run these via Supabase dashboard > Storage OR via SQL
-- ============================================================

-- You must create these two buckets in your Supabase Storage dashboard:
-- 1. "resume-sources"  — private — stores uploaded source files
-- 2. "tailored-pdfs"  — private — stores generated PDF outputs

-- ============================================================
-- Trigger: auto-create profile on new user signup
-- ============================================================

CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS TRIGGER AS $$
BEGIN
  INSERT INTO public.profiles (id, email)
  VALUES (NEW.id, NEW.email);
  RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

CREATE TRIGGER on_auth_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();

-- ============================================================
-- Seed: set your admin account
-- After you log in for the first time, run this:
-- UPDATE public.profiles SET is_admin = TRUE WHERE email = 'enollc21@gmail.com';
-- ============================================================
