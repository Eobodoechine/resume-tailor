# Keeping the Render Instance Warm

## The Problem

On Render's free plan, web services sleep after **15 minutes of inactivity**.
The first request after sleep takes 30–60 seconds, which looks like a broken app
to a new user.

The app already has a `/api/health` endpoint that returns `{"status": "ok"}` in
under 10ms — it's designed specifically for this.

## Free Fix: cron-job.org

1. Go to **https://cron-job.org** and create a free account.
2. Click **Create cronjob**.
3. Set:
   - **URL**: `https://resume-tailor-ogop.onrender.com/api/health`
   - **Schedule**: Every 10 minutes (`*/10 * * * *`)
   - **Request method**: GET
4. Save. The service will ping `/api/health` every 10 minutes, keeping the
   dyno warm at no cost.

## Paid Fix: Render Starter ($7/month)

Upgrading from the free plan to the Starter plan disables sleep entirely.
No cron job needed. Worth it once the app has regular users.

Upgrade at: https://dashboard.render.com → Your service → Settings → Instance Type
