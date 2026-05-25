# Resume Tailor — Setup Guide

## What you need (all free)
- GitHub account (free)
- Supabase account (free) — supabase.com
- Render account (free) — render.com
- Anthropic API key — platform.claude.com

---

## Step 1 — Supabase Setup (~10 min)

1. Go to **supabase.com** → New Project
2. Pick a name (e.g. "resume-tailor"), set a password, choose a region near you
3. Once created, go to **SQL Editor** → paste the entire contents of `supabase/schema.sql` → Run
4. Go to **Storage** → Create two buckets:
   - `resume-sources` (private)
   - `tailored-pdfs` (private)
5. Go to **Settings → API** → copy:
   - Project URL → this is your `SUPABASE_URL`
   - `anon` `public` key → this is your `SUPABASE_ANON_KEY`
   - `service_role` key → this is your `SUPABASE_SERVICE_KEY` *(keep this secret)*
6. Go to **Authentication → Providers → Google** (optional, see Google Sign-In section below)

---

## Step 2 — Anthropic API Key (~5 min)

1. Go to **platform.claude.com** → Sign In
2. API Keys → Create New Key
3. Copy it — this is your `ANTHROPIC_API_KEY`

---

## Step 3 — GitHub (~5 min)

1. Create a new **private** GitHub repo (e.g. `resume-tailor`)
2. On your computer, open Terminal and run:

```bash
cd "/Users/eobodoechine/Documents/Claude/Projects/Job Applier (1)/resume-tailor-app"
git init
git add .
git commit -m "Initial build"
git remote add origin https://github.com/YOUR_USERNAME/resume-tailor.git
git push -u origin main
```

---

## Step 4 — Deploy to Render (~10 min)

1. Go to **render.com** → New → Web Service
2. Connect your GitHub account → select the `resume-tailor` repo
3. Render will detect the Dockerfile automatically
4. Set Environment Variables (one at a time):
   - `SUPABASE_URL` → your value
   - `SUPABASE_ANON_KEY` → your value
   - `SUPABASE_SERVICE_KEY` → your value
   - `ANTHROPIC_API_KEY` → your value
   - `ADMIN_EMAIL` → enollc21@gmail.com
5. Click **Deploy**
6. Wait ~5 minutes for the first build
7. Render gives you a URL like `https://resume-tailor-xxxx.onrender.com`

---

## Step 5 — Set yourself as admin

After your first login, run this in Supabase SQL Editor:

```sql
UPDATE public.profiles SET is_admin = TRUE WHERE email = 'enollc21@gmail.com';
```

Then go to `https://your-app.onrender.com/admin-panel` to approve users.

---

## Google Sign-In (Optional — same cost, easier for users)

1. In Supabase: **Authentication → Providers → Google** → enable it
2. Follow the Google OAuth setup instructions Supabase shows
3. Update `index.html` line: `const SUPABASE_URL = "https://your-project.supabase.co";`
4. Push the change to GitHub → Render redeploys automatically

Google sign-in costs **nothing extra** — Supabase Auth is free regardless of provider.

---

## Cost summary

| Item | Cost |
|---|---|
| Hosting (Render) | Free |
| Supabase (DB + Storage + Auth) | Free |
| Claude Haiku API | ~$0.01–0.05 per session |

For 50 users/month doing 2 sessions each: ~$1–5/month total.

---

## Your existing Cowork skill

The Cowork skill is **not replaced** by this app. The web app is for people who don't have Cowork.
You still use the skill in Cowork for your own job search — it reads your local files directly.
The web app is a separate, standalone product for others.
