# Reels Scheduler

Upload Reels once; each goes to the next free posting time on every Facebook Page you pick, and Facebook posts it at that time.

Render settings:
- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn app:app --host 0.0.0.0 --port $PORT`

Environment variables: `ADMIN_PASSWORD`, `SECRET_KEY`, `DATABASE_URL`, `FB_APP_ID`, `FB_APP_SECRET`, `PUBLIC_URL`, optional `TIMEZONE` (default Asia/Manila).
