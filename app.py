"""Reels Scheduler: upload Reels once, and Facebook posts them to your Pages at the times you set.

Environment:
  ADMIN_PASSWORD   password for this site (required)
  SECRET_KEY       long random text; keeps logins and saved Facebook keys safe (required, don't change it later)
  DATABASE_URL     Postgres link (Neon). Without it a local file is used, which Render wipes on every deploy.
  FB_APP_ID, FB_APP_SECRET   from your Meta app (developers.facebook.com)
  PUBLIC_URL       this site's address, e.g. https://reels-scheduler.onrender.com
  TIMEZONE         default Asia/Manila
"""
import asyncio
import base64
import datetime as dt
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import shutil
import time
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet, InvalidToken
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Route
from starlette.templating import Jinja2Templates

import db
import fb

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("scheduler")

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
SECRET_KEY = os.getenv("SECRET_KEY", "") or secrets.token_hex(32)
TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Manila"))
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "/tmp/reels-uploads")
MAX_VIDEO = int(os.getenv("MAX_VIDEO_MB", "500")) * 1024 * 1024
CHUNK_MAX = 9 * 1024 * 1024
DAILY_LIMIT = 30           # Facebook: 30 Reels per Page per 24 hours through apps
LEAD_MIN = 20              # first free time must be at least this many minutes away
MAX_DAYS = 28              # Facebook schedules Reels up to about a month ahead
CAPTION_MAX = 2200
MAX_THUMB = 8 * 1024 * 1024
THUMB_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/jpg": ".jpg", "image/webp": ".webp"}

os.makedirs(UPLOAD_DIR, exist_ok=True)
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
run = asyncio.to_thread

_fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256((SECRET_KEY + "|fb-tokens").encode()).digest()))


def enc(s: str) -> str:
    return _fernet.encrypt(s.encode()).decode()


def dec(s: str) -> str:
    try:
        return _fernet.decrypt(s.encode()).decode() if s else ""
    except InvalidToken:
        return ""


# ---------- small helpers ----------
def authed(request) -> bool:
    return bool(request.session.get("ok"))


def csrf_token(request) -> str:
    if "csrf" not in request.session:
        request.session["csrf"] = secrets.token_urlsafe(16)
    return request.session["csrf"]


def redirect(url: str):
    return RedirectResponse(url, status_code=303)


def flash(request, msg: str):
    request.session["flash"] = msg


def page(request, name: str, **ctx):
    ctx.update(request=request, csrf=csrf_token(request), flash=request.session.pop("flash", None),
               nav=request.url.path)
    return templates.TemplateResponse(request, name, ctx)


async def guarded_post(request):
    if not authed(request):
        return None, redirect("/login")
    form = await request.form()
    if not hmac.compare_digest(str(form.get("csrf", "")), request.session.get("csrf", "")):
        return None, PlainTextResponse("This page was open too long. Go back and reload it.", status_code=400)
    return form, None


def api_ok(request) -> bool:
    """Logged in, same site, and the page's CSRF token in the X-CSRF header."""
    if not authed(request):
        return False
    if request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
        return False
    return hmac.compare_digest(request.headers.get("x-csrf", ""), request.session.get("csrf", "") or "-")


def parse_int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def public_url(request) -> str:
    u = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
    if u:
        return u
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    return f"{proto}://{request.url.netloc}"


def redirect_uri(request) -> str:
    return public_url(request) + "/fb/callback"


_TRIES: dict = {}


def too_many(key: str, limit: int = 8, window: int = 600) -> bool:
    now = time.time()
    hits = [t for t in _TRIES.get(key, []) if now - t < window]
    hits.append(now)
    _TRIES[key] = hits
    return len(hits) > limit


def client_ip(request) -> str:
    return (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else "?"))


# ---------- time slots ----------
_SLOT_RX = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*$", re.I)


def parse_slots(text: str) -> tuple:
    """'9am, 1:30 pm, 20:00' -> ([(9,0), (13,30), (20,0)], bad_parts)."""
    out, bad = set(), []
    for part in re.split(r"[,;\n]+", text or ""):
        if not part.strip():
            continue
        m = _SLOT_RX.match(part)
        if not m:
            bad.append(part.strip())
            continue
        h, mi, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
        if ap:
            if not 1 <= h <= 12:
                bad.append(part.strip())
                continue
            h = (h % 12) + (12 if ap == "pm" else 0)
        if h > 23 or mi > 59:
            bad.append(part.strip())
            continue
        out.add((h, mi))
    return sorted(out), bad


def slots_text(slots: list) -> str:
    return ", ".join(f"{h:02d}:{m:02d}" for h, m in slots)


def fmt_time(h: int, m: int) -> str:
    return dt.time(h, m).strftime("%I:%M %p").lstrip("0")


def next_free(page_slots: str, taken: set, count: int = 1, now: dt.datetime | None = None) -> list:
    """The next `count` free posting times (UTC, naive) for a Page."""
    slots, _ = parse_slots(page_slots)
    if not slots:
        return []
    now = now or db.utcnow()
    earliest = now + dt.timedelta(minutes=LEAD_MIN)
    latest = now + dt.timedelta(days=MAX_DAYS)
    today = now.replace(tzinfo=dt.timezone.utc).astimezone(TZ).date()
    out = []
    for d in range(MAX_DAYS + 1):
        day = today + dt.timedelta(days=d)
        for h, m in slots:
            t = dt.datetime(day.year, day.month, day.day, h, m, tzinfo=TZ).astimezone(dt.timezone.utc).replace(tzinfo=None)
            if earliest <= t <= latest and t not in taken:
                out.append(t)
                taken = taken | {t}
                if len(out) >= count:
                    return out
    return out


def parse_local(text: str) -> dt.datetime | None:
    """'2026-11-20T21:00' in the site's timezone -> UTC (naive), or None."""
    try:
        t = dt.datetime.strptime(str(text).strip()[:16], "%Y-%m-%dT%H:%M")
    except ValueError:
        return None
    return t.replace(tzinfo=TZ).astimezone(dt.timezone.utc).replace(tzinfo=None)


def local(t: dt.datetime | None) -> dt.datetime | None:
    return t.replace(tzinfo=dt.timezone.utc).astimezone(TZ) if t else None


def when_text(t: dt.datetime | None) -> str:
    if t is None:
        return "Right away"
    lt, today = local(t), dt.datetime.now(TZ).date()
    day = ("Today" if lt.date() == today else "Tomorrow" if lt.date() == today + dt.timedelta(days=1)
           else lt.strftime("%a %b ") + str(lt.day))
    return f"{day}, {fmt_time(lt.hour, lt.minute)}"


templates.env.globals.update(when_text=when_text, fmt_time=fmt_time, parse_slots=parse_slots)


# ---------- login ----------
async def login_page(request):
    if authed(request):
        return redirect("/")
    return page(request, "login.html", need_pw=not ADMIN_PASSWORD)


async def login_submit(request):
    form = await request.form()
    if not hmac.compare_digest(str(form.get("csrf", "")), request.session.get("csrf", "") or "-"):
        return redirect("/login")
    if too_many("login:" + client_ip(request), limit=8, window=600):
        flash(request, "Too many tries. Wait 10 minutes and try again.")
        return redirect("/login")
    if ADMIN_PASSWORD and hmac.compare_digest(str(form.get("password", "")), ADMIN_PASSWORD):
        request.session.clear()
        request.session["ok"] = True
        return redirect("/")
    flash(request, "Wrong password.")
    return redirect("/login")


async def logout(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    request.session.clear()
    return redirect("/login")


# ---------- connecting Facebook ----------
async def fb_connect(request):
    if not authed(request):
        return redirect("/login")
    if not (fb.app_id() and fb.app_secret()):
        flash(request, "Add FB_APP_ID and FB_APP_SECRET on Render first (see the steps on this page).")
        return redirect("/pages")
    state = secrets.token_urlsafe(24)
    request.session["oauth_state"] = state
    return RedirectResponse(fb.dialog_url(redirect_uri(request), state), status_code=302)


async def fb_callback(request):
    if not authed(request):
        return redirect("/login")
    q = request.query_params
    state = request.session.pop("oauth_state", "")
    if not state or not hmac.compare_digest(q.get("state", ""), state):
        flash(request, "That connection link expired. Tap Connect a Facebook account again.")
        return redirect("/pages")
    if q.get("error"):
        flash(request, "Facebook connection was cancelled.")
        return redirect("/pages")
    try:
        short = await fb.exchange_code(q.get("code", ""), redirect_uri(request))
        token = await fb.long_lived(short)
        who = await fb.me(token)
        found = await fb.pages(token)
    except fb.FBError as e:
        log.warning("Facebook connect failed: %s", e.message)
        flash(request, f"Facebook said: {e.message}")
        return redirect("/pages")
    rows = [{"fb_page_id": p["id"], "name": p["name"], "picture": p["picture"], "token_enc": enc(p["access_token"])}
            for p in found]
    acc, new, upd = await run(db.save_account, str(who.get("id")), who.get("name", ""), enc(token), rows)
    if not found:
        flash(request, f"Connected {acc.name}, but Facebook didn't share any Pages. Connect again and tick every Page "
                       "on Facebook's screen.")
    else:
        flash(request, f"Connected {acc.name}: {len(found)} Page{'s' if len(found) != 1 else ''}"
                       + (f", {new} new" if new and upd else "") + ".")
    return redirect("/pages")


async def account_remove(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    aid = parse_int(form.get("id"))
    if aid:
        await run(db.remove_account, aid)
        flash(request, "Account removed. Its Pages can't be posted to until you connect it again.")
    return redirect("/pages")


# ---------- Pages ----------
async def pages_page(request):
    if not authed(request):
        return redirect("/login")
    accounts = await run(db.list_accounts)
    pages = await run(db.list_pages)
    names = {a.id: a.name for a in accounts}
    rows, hidden = [], 0
    for p in pages:
        if p.gone or not p.token_enc:   # no connected account can post to it: keep its settings, just don't list it
            hidden += 1
            continue
        nxt = next_free(p.slots, await run(db.taken_times, p.id), 1) if p.active else []
        rows.append({"p": p, "account": names.get(p.account_id, ""), "next": when_text(nxt[0]) if nxt else ""})
    return page(request, "pages.html", accounts=accounts, rows=rows, hidden=hidden, ready=bool(fb.app_id() and fb.app_secret()),
                redirect_uri=redirect_uri(request), site=public_url(request), tz=str(TZ.key))


async def page_save(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    pid = parse_int(form.get("id"))
    p = await run(db.get_page, pid) if pid else None
    if p is None:
        return redirect("/pages")
    slots, bad = parse_slots(str(form.get("slots", "")))
    if bad:
        flash(request, f"{p.name}: I couldn't read {', '.join(bad[:3])}. Use times like 9:00, 1:30 PM or 20:00.")
        return redirect(f"/pages#p{pid}")
    tags = " ".join(str(form.get("tags", "")).split())[:500]
    await run(db.update_page, pid, slots_text(slots), tags, bool(form.get("active")))
    flash(request, f"{p.name} saved." + ("" if slots else " It has no posting times, so the queue will skip it."))
    return redirect(f"/pages#p{pid}")


# ---------- uploading ----------
UPLOADS: dict = {}          # upload id -> {name, size, path, at}
BG: set = set()             # background jobs (kept here so they aren't dropped before they finish)


def spawn(coro) -> None:
    task = asyncio.create_task(coro)
    BG.add(task)
    task.add_done_callback(BG.discard)
RUNNING: set = set()        # post ids being sent right now
SEND_LOCK = asyncio.Semaphore(1)


def _cleanup_files(max_age: int = 3 * 3600) -> None:
    now = time.time()
    for uid, u in list(UPLOADS.items()):
        if now - u["at"] > max_age:
            UPLOADS.pop(uid, None)
            try:
                os.remove(u["path"])
            except OSError:
                pass
            thumb = u.get("thumb")
            if thumb:
                try:
                    os.remove(thumb["path"])
                except OSError:
                    pass
    try:
        for name in os.listdir(UPLOAD_DIR):
            path = os.path.join(UPLOAD_DIR, name)
            if now - os.path.getmtime(path) > max_age:
                os.remove(path)
    except OSError:
        pass


async def upload_init(request):
    if not api_ok(request):
        return JSONResponse({"ok": False, "msg": "Log in again."}, status_code=401)
    try:
        data = json.loads(await request.body())
        name = re.sub(r"[^\w .()\-]+", "", str(data.get("name", "video.mp4")))[:120] or "video.mp4"
        size = int(data.get("size", 0))
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "msg": "Bad request."}, status_code=400)
    if size <= 0:
        return JSONResponse({"ok": False, "msg": "That file is empty."}, status_code=400)
    if size > MAX_VIDEO:
        return JSONResponse({"ok": False, "msg": f"That video is over {MAX_VIDEO // 1024 // 1024} MB."}, status_code=413)
    free = shutil.disk_usage(UPLOAD_DIR).free
    if size > free - 200 * 1024 * 1024:
        return JSONResponse({"ok": False, "msg": "The server is out of space right now. Wait a few minutes."}, status_code=507)
    _cleanup_files()
    uid = secrets.token_urlsafe(12)
    path = os.path.join(UPLOAD_DIR, uid + ".mp4")
    open(path, "wb").close()
    UPLOADS[uid] = {"name": name, "size": size, "path": path, "at": time.time()}
    return JSONResponse({"ok": True, "id": uid, "chunk": 8 * 1024 * 1024})


async def upload_chunk(request):
    if not api_ok(request):
        return JSONResponse({"ok": False, "msg": "Log in again."}, status_code=401)
    u = UPLOADS.get(request.path_params["uid"])
    if u is None:
        return JSONResponse({"ok": False, "msg": "The server restarted. Upload this video again."}, status_code=404)
    offset = parse_int(request.query_params.get("offset"))
    have = os.path.getsize(u["path"])
    if offset != have:
        return JSONResponse({"ok": False, "have": have}, status_code=409)
    got = 0
    with open(u["path"], "ab") as f:
        async for part in request.stream():
            got += len(part)
            if got > CHUNK_MAX or have + got > u["size"]:
                f.truncate(have)
                return JSONResponse({"ok": False, "msg": "Bad chunk."}, status_code=400)
            f.write(part)
    u["at"] = time.time()
    return JSONResponse({"ok": True, "have": have + got})


async def upload_thumb(request):
    """Optional custom cover photo for one uploaded video (small, single shot, no chunking needed)."""
    if not api_ok(request):
        return JSONResponse({"ok": False, "msg": "Log in again."}, status_code=401)
    u = UPLOADS.get(request.path_params["uid"])
    if u is None:
        return JSONResponse({"ok": False, "msg": "The server restarted. Upload the video again."}, status_code=404)
    form = await request.form()
    file = form.get("thumb")
    if file is None or not getattr(file, "filename", ""):
        return JSONResponse({"ok": False, "msg": "No image received."}, status_code=400)
    ext = THUMB_EXT.get((file.content_type or "").lower())
    if not ext:
        return JSONResponse({"ok": False, "msg": "Use a JPG, PNG, or WEBP image for the thumbnail."}, status_code=400)
    body = await file.read()
    if not body:
        return JSONResponse({"ok": False, "msg": "That image is empty."}, status_code=400)
    if len(body) > MAX_THUMB:
        return JSONResponse({"ok": False, "msg": f"That image is over {MAX_THUMB // 1024 // 1024} MB."}, status_code=413)
    old = u.get("thumb")
    name = u["name"].rsplit(".", 1)[0] + "-thumb" + ext if "." in u["name"] else u["name"] + "-thumb" + ext
    name = re.sub(r"[^\w .()\-]+", "", name)[:140] or ("thumb" + ext)
    path = os.path.join(UPLOAD_DIR, request.path_params["uid"] + ".thumb" + ext)
    with open(path, "wb") as f:
        f.write(body)
    if old and old["path"] != path:
        try:
            os.remove(old["path"])
        except OSError:
            pass
    u["thumb"] = {"path": path, "name": name}
    u["at"] = time.time()
    return JSONResponse({"ok": True, "name": name})


async def upload_done(request):
    """The whole file is here: make one post per chosen Page, give each a time, and send them to Facebook."""
    if not api_ok(request):
        return JSONResponse({"ok": False, "msg": "Log in again."}, status_code=401)
    uid = request.path_params["uid"]
    u = UPLOADS.get(uid)
    if u is None:
        return JSONResponse({"ok": False, "msg": "The server restarted. Upload this video again."}, status_code=404)
    if os.path.getsize(u["path"]) != u["size"]:
        return JSONResponse({"ok": False, "msg": "The upload didn't finish. Try again."}, status_code=400)
    try:
        data = json.loads(await request.body())
        page_ids = [int(x) for x in data.get("pages", [])][:200]
        caption = str(data.get("caption", "")).strip()[:CAPTION_MAX]
        mode = str(data.get("when", "queue"))
        now_mode = mode == "now"
        at_text = str(data.get("at", ""))
        ai = bool(data.get("ai", True))
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "msg": "Bad request."}, status_code=400)
    at = None
    if mode == "at":                     # a date and time picked for this video
        at = parse_local(at_text)
        if at is None:
            return JSONResponse({"ok": False, "msg": "Pick a date and time for this video."}, status_code=400)
        if at < db.utcnow() + dt.timedelta(minutes=15):
            return JSONResponse({"ok": False, "msg": "Pick a time at least 15 minutes from now."}, status_code=400)
        if at > db.utcnow() + dt.timedelta(days=MAX_DAYS):
            return JSONResponse({"ok": False, "msg": f"Facebook schedules up to {MAX_DAYS} days ahead. "
                                                     "Pick an earlier date."}, status_code=400)
    pages = {p.id: p for p in await run(db.list_pages, True)}
    chosen = [pages[i] for i in dict.fromkeys(page_ids) if i in pages]
    if not chosen:
        return JSONResponse({"ok": False, "msg": "Pick at least one Page."}, status_code=400)
    rows, plan, skipped = [], [], []
    for p in chosen:
        if await run(db.sent_last_day, p.id) >= DAILY_LIMIT:
            skipped.append(f"{p.name} (Facebook allows {DAILY_LIMIT} Reels a day per Page)")
            continue
        if now_mode:
            when = None
        elif at is not None:
            when = at
        else:
            free = next_free(p.slots, await run(db.taken_times, p.id), 1)
            if not free:
                skipped.append(f"{p.name} (no free posting time in the next {MAX_DAYS} days; add more times)")
                continue
            when = free[0]
        text = caption + (("\n\n" + p.tags) if p.tags and p.tags not in caption else "")
        rows.append({"page_id": p.id, "batch": uid, "file_name": u["name"], "caption": text[:CAPTION_MAX],
                     "scheduled_at": when, "ai": ai, "status": "waiting",
                     "thumb_name": (u.get("thumb") or {}).get("name", "")})
        plan.append({"page": p.name, "when": when_text(when)})
    if not rows:
        return JSONResponse({"ok": False, "msg": "Couldn't schedule it: " + "; ".join(skipped)}, status_code=400)
    ids = await run(db.add_posts, rows)
    RUNNING.update(ids)
    thumb_path = (u.get("thumb") or {}).get("path", "")
    UPLOADS.pop(uid, None)
    spawn(_send(u["path"], u["size"], ids, thumb_path))
    return JSONResponse({"ok": True, "ids": ids, "plan": plan, "skipped": skipped})


def _fb_problem(e: "fb.FBError") -> str:
    if e.token_bad:
        return "Facebook rejected the Page's key. Reconnect the account on the Pages tab, then upload again."
    if e.code == 613:
        return f"Facebook's limit of {DAILY_LIMIT} Reels a day for this Page was reached. Try again tomorrow."
    if e.code == 200:
        return "This account isn't allowed to post on this Page anymore. Check your Page role, then reconnect."
    if e.code == 368:
        return "Facebook blocked this post as against its rules."
    return f"Facebook said: {e.message}"


async def _send(path: str, size: int, ids: list, thumb_path: str = "") -> None:
    """Send one uploaded video to every Page it was scheduled for, one after another."""
    try:
        async with SEND_LOCK:
            for post in await run(db.get_posts, ids):
                p = await run(db.get_page, post.page_id)
                token = dec(p.token_enc) if p else ""
                if not token:
                    await run(db.update_post, post.id, status="failed", error="This Page isn't connected anymore.")
                    continue
                await run(db.update_post, post.id, status="sending")
                when_ts = None
                if post.scheduled_at is not None:
                    if post.scheduled_at < db.utcnow() + dt.timedelta(minutes=11):
                        # the upload took so long that this time is too close: move it 15 minutes from now
                        soon = db.utcnow() + dt.timedelta(minutes=15)
                        soon = soon.replace(second=0, microsecond=0) + dt.timedelta(minutes=(5 - soon.minute % 5) % 5)
                        await run(db.update_post, post.id, scheduled_at=soon)
                        post.scheduled_at = soon
                    when_ts = int(post.scheduled_at.replace(tzinfo=dt.timezone.utc).timestamp())
                try:
                    video_id, upload_url = await fb.reel_start(p.fb_page_id, token)
                    await run(db.update_post, post.id, video_id=video_id)
                    await fb.reel_upload(upload_url, token, path, size)
                    await fb.reel_finish(p.fb_page_id, token, video_id, post.caption, when_ts, post.ai)
                    note = ""
                    if thumb_path and post.thumb_name:
                        try:
                            await fb.set_thumbnail(video_id, token, thumb_path)
                        except fb.FBError as te:
                            # not fatal: the Reel is still scheduled, just with Facebook's own auto cover
                            log.warning("Thumbnail for page %s didn't take: %s", p.fb_page_id, te.message)
                            note = ("Thumbnail wasn't accepted, so Facebook will pick the cover. "
                                    f"Facebook said: {te.message}")[:500]
                    await run(db.update_post, post.id, status="scheduled", sent_at=db.utcnow(), error=note)
                except fb.FBError as e:
                    log.warning("Reel for page %s failed: %s (code %s)", p.fb_page_id, e.message, e.code)
                    await run(db.update_post, post.id, status="failed", error=_fb_problem(e)[:500])
                    if e.token_bad:
                        await run(db.set_account_problem, p.account_id, "Facebook rejected a key. Reconnect this account.")
                except Exception:
                    log.exception("Reel upload crashed")
                    await run(db.update_post, post.id, status="failed", error="Something went wrong while sending. Upload again.")
    finally:
        RUNNING.difference_update(ids)
        try:
            os.remove(path)
        except OSError:
            pass
        if thumb_path:
            try:
                os.remove(thumb_path)
            except OSError:
                pass


async def _check_live() -> None:
    """Ask Facebook about Reels whose time has passed: posted, or failed?"""
    for post in await run(db.posts_to_check):
        p = await run(db.get_page, post.page_id)
        token = dec(p.token_enc) if p else ""
        if not token or not post.video_id:
            continue
        try:
            st = (await fb.video_status(post.video_id, token)).get("status") or {}
        except fb.FBError as e:
            await run(db.update_post, post.id, checked_at=db.utcnow())
            if e.code == 100:
                await run(db.update_post, post.id, status="failed", error="Facebook can't find this Reel anymore.")
            continue
        proc = st.get("processing_phase") or {}
        pub = st.get("publishing_phase") or {}
        if str(st.get("video_status", "")).lower() in ("error", "upload_failed") or proc.get("status") == "error":
            errs = proc.get("errors") or [{}]
            await run(db.update_post, post.id, status="failed", checked_at=db.utcnow(),
                      error=("Facebook couldn't process this video: " + str(errs[0].get("message", "unknown problem")))[:500])
        elif str(pub.get("publish_status", "")).lower() == "published" or pub.get("status") == "complete":
            await run(db.update_post, post.id, status="posted", checked_at=db.utcnow())
        else:
            await run(db.update_post, post.id, checked_at=db.utcnow())


async def _mark_stale() -> None:
    for post in await run(db.stale_sending):
        if post.id not in RUNNING:
            await run(db.update_post, post.id, status="failed",
                      error="The upload was interrupted (the server restarted). Upload this video again.")


# ---------- queue ----------
def _post_row(post, pages: dict) -> dict:
    p = pages.get(post.page_id)
    first = (post.caption or "").strip().split("\n")[0]
    return {"id": post.id, "page": p.name if p else "(removed Page)", "pic": p.picture if p else "",
            "when": when_text(post.scheduled_at), "day": local(post.scheduled_at).date().isoformat() if post.scheduled_at else "",
            "caption": first[:140], "file": post.file_name, "status": post.status, "error": post.error,
            "ai": post.ai}


def _day_title(iso: str) -> str:
    if not iso:
        return "Posting right away"
    d, today = dt.date.fromisoformat(iso), dt.datetime.now(TZ).date()
    if d == today:
        return "Today"
    if d == today + dt.timedelta(days=1):
        return "Tomorrow"
    return d.strftime("%A, %B ") + str(d.day)


async def queue_page(request):
    if not authed(request):
        return redirect("/login")
    await _mark_stale()
    spawn(_check_live())
    pages = await run(db.list_pages)
    by_id = {p.id: p for p in pages}
    accounts = {a.id: a.name for a in await run(db.list_accounts)}
    usable = [p for p in pages if p.active and not p.gone and p.token_enc]
    groups: dict = {}
    for p in usable:
        groups.setdefault(accounts.get(p.account_id, "Facebook"), []).append(
            {"id": p.id, "name": p.name, "pic": p.picture, "slots": bool(parse_slots(p.slots)[0])})
    days: dict = {}
    for post in await run(db.upcoming_posts):
        r = _post_row(post, by_id)
        days.setdefault(r["day"], []).append(r)
    recent = [_post_row(x, by_id) for x in await run(db.recent_posts)]
    return page(request, "queue.html", groups=groups, days=[(_day_title(k), v) for k, v in days.items()],
                recent=recent, tz=str(TZ.key), has_pages=bool(usable), max_mb=MAX_VIDEO // 1024 // 1024,
                max_days=MAX_DAYS)


async def posts_api(request):
    if not authed(request):
        return JSONResponse({"ok": False}, status_code=401)
    ids = [int(x) for x in request.query_params.get("ids", "").split(",") if x.strip().isdigit()][:200]
    by_id = {p.id: p for p in await run(db.list_pages)}
    return JSONResponse({"ok": True, "posts": [_post_row(p, by_id) for p in await run(db.get_posts, ids)]},
                        headers={"Cache-Control": "no-store"})


async def post_cancel(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    pid = parse_int(form.get("id"))
    post = (await run(db.get_posts, [pid]) or [None])[0] if pid else None
    if post is None or post.status not in ("waiting", "scheduled"):
        flash(request, "That post can't be cancelled now (it's being sent or already done).")
        return redirect("/")
    if post.status == "scheduled" and post.video_id:
        p = await run(db.get_page, post.page_id)
        try:
            await fb.delete_video(post.video_id, dec(p.token_enc) if p else "")
        except fb.FBError as e:
            flash(request, f"Couldn't remove it from Facebook: {e.message}. Delete it in Meta Business Suite.")
            return redirect("/")
    await run(db.update_post, pid, status="canceled", error="")
    flash(request, "Cancelled. Its time is free again.")
    return redirect("/")


async def clear_history(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    n = await run(db.clear_finished)
    flash(request, f"Cleared {n} finished post{'s' if n != 1 else ''} from the list.")
    return redirect("/")


async def health(request):
    return PlainTextResponse("ok")


async def privacy_page(request):
    return page(request, "legal.html", kind="privacy", title="Privacy Policy", updated="November 19, 2026")


async def deletion_page(request):
    return page(request, "legal.html", kind="deletion", title="Delete your data", updated="November 19, 2026",
                contact=os.getenv("CONTACT_EMAIL", "").strip())


# ---------- security headers ----------
# ---------- analytics ----------
STAT_METRICS = {"views": "page_media_view", "viewers": "page_total_media_view_unique",
                "video_views": "page_video_views", "engagements": "page_post_engagements",
                "follows": "page_daily_follows_unique"}
EARN_METRIC = "content_monetization_earnings"
STALE = dt.timedelta(hours=6)         # numbers older than this refresh by themselves when you open Analytics
RANGES = (7, 28, 90)
ANALYTICS = {"running": False, "done": 0, "total": 0, "page": "", "error": ""}
STATUS = {"ok": ("ok", "✓", "OK"), "warning": ("time", "!", "Warning"), "restricted": ("bad", "✕", "Restricted"),
          "suspended": ("bad", "✕", "Suspended"), "": ("", "?", "Unknown")}
RESTRICTION_TEXT = {"page_publish": "Page is unpublished", "page_read_only": "Can't post or make changes",
                    "page_messaging": "Can't send messages", "page_messaging_api": "Can't send messages from apps"}
MINUS = "\u2212"
CURRENCY_SIGN = {"USD": "$", "PHP": "₱", "EUR": "€", "GBP": "£", "JPY": "¥"}


def _num(v) -> int | None:
    """An insight value as a whole number. Breakdowns (per reaction, comments + shares) are added up."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        try:
            return int(float(v))
        except ValueError:
            return None
    parts = [_num(x) for x in (v.values() if isinstance(v, dict) else v if isinstance(v, list) else [])]
    parts = [x for x in parts if x is not None]
    return sum(parts) if parts else None


def _money(v) -> tuple:
    """An earnings value as (micros, currency); 1,000,000 micros = 1.00. Facebook sends a number or an object
    like {"currency": "USD", "micro_amount": 1234567}; a breakdown by content type is added up."""
    if isinstance(v, bool) or v is None:
        return None, ""
    if isinstance(v, (int, float)):
        return round(v * 1_000_000), ""
    if isinstance(v, str):
        try:
            return round(float(v) * 1_000_000), ""
        except ValueError:
            return None, ""
    if isinstance(v, dict):
        cur = str(v.get("currency") or "")[:3].upper()
        for k in ("micro_amount", "amount_micros", "micros"):
            if isinstance(v.get(k), (int, float, str)) and not isinstance(v.get(k), bool):
                try:
                    return int(float(v[k])), cur
                except ValueError:
                    pass
        for k in ("total", "amount", "value"):
            if k in v:
                m, c = _money(v[k])
                if m is not None:
                    return m, c or cur
        items = list(v.values())
    elif isinstance(v, list):
        cur, items = "", v
    else:
        return None, ""
    total, seen = 0, False
    for x in items:
        if isinstance(x, (dict, list)):
            m, c = _money(x)
            if m is not None:
                total, seen, cur = total + m, True, cur or c
    return (total, cur) if seen else (None, cur)


def _fb_time(s) -> dt.datetime | None:
    try:
        t = dt.datetime.fromisoformat(str(s).replace("+0000", "+00:00"))
    except ValueError:
        return None
    return t.astimezone(dt.timezone.utc).replace(tzinfo=None) if t.tzinfo else t


def _slow_down(e: fb.FBError) -> bool:
    return e.code in (4, 17, 32, 613) or "rate limit" in e.message.lower()


def _no_permission(e: fb.FBError) -> bool:
    return e.code in (10, 200, 283) or (e.code == 100 and "permission" in e.message.lower())


def _bad_metric(e: fb.FBError) -> bool:
    return e.code == 100 and "metric" in e.message.lower() and "permission" not in e.message.lower()


def _why(e: fb.FBError, perm: str) -> str:
    if e.token_bad:
        return "Facebook logged this account out. Reconnect it on the Pages tab."
    if _no_permission(e):
        return (f"Facebook needs the {perm} permission. Add it to your Facebook Login setup in Meta, "
                "then connect this account again on the Pages tab.")
    return "Facebook said: " + e.message[:200]


async def _reel_row(r: dict, token: str) -> tuple:
    vid = str(r.get("id") or "")
    note, ins = "", {}
    try:
        ins = await fb.reel_insights(vid, token)
    except fb.FBError as e:
        if _slow_down(e):
            raise
        note = _why(e, "read_insights")
    url = str(r.get("permalink_url") or "")
    if url.startswith("/"):
        url = "https://www.facebook.com" + url
    return {"video_id": vid, "title": " ".join(str(r.get("description") or "").split())[:300], "url": url[:400],
            "created": _fb_time(r.get("created_time")), "plays": _num(ins.get("blue_reels_play_count")),
            "reach": _num(ins.get("post_impressions_unique")), "avg_ms": _num(ins.get("post_video_avg_time_watched")),
            "likes": _num(ins.get("post_video_likes_by_reaction_type")),
            "social": _num(ins.get("post_video_social_actions")), "follows": _num(ins.get("post_video_followers")),
            "checked_at": db.utcnow()}, note


async def _refresh_page(p) -> None:
    """Fetch one Page's last 90 days, its standing with Facebook, and its latest Reels."""
    token = dec(p.token_enc)
    today = dt.datetime.now(dt.timezone.utc).date()
    since, until = (today - dt.timedelta(days=90)).isoformat(), (today + dt.timedelta(days=1)).isoformat()
    days, stats_note = {}, ""
    for field, metric in STAT_METRICS.items():
        try:
            got = await fb.page_insight(p.fb_page_id, token, metric, since, until)
        except fb.FBError as e:
            if _slow_down(e):
                raise
            if _bad_metric(e):
                continue                # Facebook doesn't offer this number (any more); show the rest
            stats_note = _why(e, "read_insights")
            break
        for day, v in got.items():
            days.setdefault(day, {})[field] = _num(v)
    earn_note = ""
    if not stats_note:
        try:
            got = await fb.page_insight(p.fb_page_id, token, EARN_METRIC, since, until)
            for day, v in got.items():
                m, cur = _money(v)
                if m is not None:
                    days.setdefault(day, {}).update(earn_micros=m, currency=cur)
            if not got:
                earn_note = "No earnings yet. The Page may not be in Facebook's content monetization program."
        except fb.FBError as e:
            if _slow_down(e):
                raise
            earn_note = (_why(e, "read_insights") if e.token_bad else
                         "Earnings aren't available. The Page may not be monetized, or this account isn't its admin.")
    await run(db.save_stat_days, p.id, days)
    health = {"status": "", "detail": "", "status_note": ""}
    try:
        st = await fb.page_status(p.fb_page_id, token)
        health["status"] = str(st.get("status") or "").lower()[:20]
        health["detail"] = json.dumps({k: st.get(k) or [] for k in ("violations", "restrictions", "recommended_actions")})
    except fb.FBError as e:
        if _slow_down(e):
            raise
        health["status_note"] = _why(e, "pages_manage_metadata")
    reels, reel_note = [], ""
    try:
        listed = [r for r in (await fb.page_reels(p.fb_page_id, token))[:25] if r.get("id")]
        for i in range(0, len(listed), 5):          # a few at a time
            for row, note in await asyncio.gather(*(_reel_row(r, token) for r in listed[i:i + 5])):
                reels.append(row)
                reel_note = reel_note or note
    except fb.FBError as e:
        if _slow_down(e):
            raise
        reel_note = _why(e, "pages_read_engagement")
    await run(db.save_reels, p.id, reels)
    await run(db.save_health, p.id, stats_note=stats_note or reel_note, earn_note=earn_note,
              checked_at=db.utcnow(), **health)


def _analytics_pages(pages: list) -> list:
    return [p for p in pages if not p.gone and p.token_enc]


async def _refresh_all(pages: list) -> None:
    try:
        for p in pages:
            ANALYTICS["page"] = p.name
            try:
                await _refresh_page(p)
            except fb.FBError as e:
                if _slow_down(e):
                    ANALYTICS["error"] = "Facebook asked to slow down. The rest will update in about an hour."
                    break
                log.warning("Analytics for %s failed: %s", p.name, e.message)
            except Exception:
                log.exception("Analytics for %s failed", p.name)
            ANALYTICS["done"] += 1
        await run(db.set_setting, "analytics_at", db.utcnow().isoformat())
    finally:
        ANALYTICS.update(running=False, page="")


def _start_refresh(pages: list) -> None:
    """Start fetching fresh numbers in the background (one run at a time)."""
    if not pages or ANALYTICS["running"]:
        return
    ANALYTICS.update(running=True, done=0, total=len(pages), page="", error="")
    spawn(_refresh_all(pages))


async def _refresh_if_old(pages: list) -> None:
    at = await run(db.get_setting, "analytics_at", "")
    try:
        old = db.utcnow() - dt.datetime.fromisoformat(at) > STALE
    except ValueError:
        old = True
    if old:
        _start_refresh(_analytics_pages(await run(db.list_pages)) if len(pages) == 1 else pages)


def _fmt_n(n) -> str:
    if n is None:
        return "–"
    n = int(n)
    if abs(n) < 10_000:
        return f"{n:,}"
    if abs(n) < 1_000_000:
        return f"{n / 1000:.1f}K".replace(".0K", "K")
    return f"{n / 1_000_000:.1f}M".replace(".0M", "M")


def _fmt_money(micros, cur: str = "") -> str:
    if micros is None:
        return "–"
    amount = micros / 1_000_000
    sign = CURRENCY_SIGN.get(cur, (cur + " ") if cur else "")
    return f"-{sign}{abs(amount):,.2f}" if amount < 0 else f"{sign}{amount:,.2f}"


def _fmt_ms(ms) -> str:
    return "–" if ms is None else f"{ms / 1000:.1f}s"


def _nice_step(v: float) -> float:
    """The smallest of 1, 2, 2.5, 5 x 10^n that is at least v."""
    if v <= 0:
        return 1
    p = 10 ** math.floor(math.log10(v))
    return next(m * p for m in (1, 2, 2.5, 5, 10) if v <= m * p)


def _chart(days: list, values: list, money: bool = False, cur: str = "") -> dict:
    """Shapes for an SVG chart in a 1000 x 300 box, plus ticks and per-day readouts (no drawing library)."""
    have = [v for v in values if v is not None]
    peak = max(have) if have else 0
    step = _nice_step(peak / 4) if peak > 0 else (1_000_000 if money else 1)
    if money and step < 10_000:
        step = 10_000                                   # never finer than one cent
    if not money:
        step = max(1, round(step))
    count = max(1, math.ceil(peak / step)) if peak > 0 else 4
    top = step * count
    compact = (not money) and top >= 10_000

    def axis(v):                                        # one style for the whole axis
        if money:
            return _fmt_money(round(v), cur)
        if not compact or v == 0:
            return f"{int(v):,}"
        return f"{v / 1_000_000:g}M" if top >= 1_000_000 else f"{v / 1000:g}K"

    def full(v):
        return _fmt_money(v, cur) if money else f"{int(v):,}"
    n = len(days)
    xs = [(i * 1000 / (n - 1)) if n > 1 else 500 for i in range(n)]
    pts = [(xs[i], 300 - v / top * 300) for i, v in enumerate(values) if v is not None]
    line = ("M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)) if pts else ""
    area = (line + f" L{pts[-1][0]:.1f},300 L{pts[0][0]:.1f},300 Z") if pts else ""
    every = max(1, round(n / 5))
    xt = [(_day_label(days[i]), xs[i] / 10) for i in range(0, n, every)] if n else []
    last = next(((i, v) for i, v in reversed(list(enumerate(values))) if v is not None), None)
    return {
        "line": line, "area": area, "empty": not have,
        "ticks": [(axis(step * k), k * 100 / count) for k in range(count + 1)],
        "xt": xt,
        "bars": [{"h": 0 if v is None else max(0, v) / top * 100} for v in values],
        "points": [{"d": _day_label(d, True), "v": "No data" if v is None else full(v)} for d, v in zip(days, values)],
        "end": ({"x": xs[last[0]] / 10, "y": 100 - last[1] / top * 100} if last else None),
    }


def _day_label(day: str, full: bool = False) -> str:
    d = dt.date.fromisoformat(day)
    return (d.strftime("%a, %b ") + str(d.day) + d.strftime(", %Y")) if full else d.strftime("%b ") + str(d.day)


def _spark(values: list) -> str:
    have = [v for v in values if v is not None]
    if len(have) < 2:
        return ""
    top, n = max(max(have), 1), len(values)
    return " ".join(f"{i * 100 / (n - 1):.1f},{30 - v / top * 28 - 1:.1f}" for i, v in enumerate(values) if v is not None)


def _health(h) -> dict:
    status = (h.status if h else "") or ""
    cls, icon, label = STATUS.get(status, STATUS[""])
    try:
        d = json.loads(h.detail) if h and h.detail else {}
    except ValueError:
        d = {}
    def when(ts):
        try:
            return local(dt.datetime.fromtimestamp(int(ts), dt.timezone.utc).replace(tzinfo=None)).strftime("%b %d, %Y %I:%M %p")
        except (TypeError, ValueError, OverflowError, OSError):
            return ""
    return {
        "status": status, "cls": cls, "icon": icon, "label": label, "note": (h.status_note if h else "") or "",
        "violations": [{"type": str(v.get("type") or "").replace("_", " ").title(), "text": str(v.get("description") or ""),
                        "url": str(v.get("url") or "")} for v in d.get("violations", []) if isinstance(v, dict)],
        "restrictions": [{"what": RESTRICTION_TEXT.get(str(r.get("feature")), str(r.get("feature") or "").replace("_", " ")),
                          "text": str(r.get("description") or ""), "since": when(r.get("applied_time")),
                          "until": when(r.get("expiration_time"))} for r in d.get("restrictions", []) if isinstance(r, dict)],
        "actions": [{"text": {"FILE_APPEAL": "File an appeal"}.get(str(a.get("action_type")), str(a.get("action_type") or "").replace("_", " ").capitalize()),
                     "url": str(a.get("url") or "")} for a in d.get("recommended_actions", []) if isinstance(a, dict)],
    }


def _build_view(pages: list, rng: int) -> dict:
    """Everything the Analytics pages show, from the saved numbers (no calls to Facebook here)."""
    ids = [p.id for p in pages]
    latest = db.latest_stat_day(ids)
    end = dt.date.fromisoformat(latest) if latest else dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)
    days = [(end - dt.timedelta(days=rng - 1 - i)).isoformat() for i in range(rng)]
    prev = [(end - dt.timedelta(days=2 * rng - 1 - i)).isoformat() for i in range(rng)]
    rows = db.stat_days(ids, prev[0])
    cell = {(r.page_id, r.day): r for r in rows}
    curs = {}
    for r in rows:
        if r.earn_micros is not None and r.day >= days[0]:
            curs[r.currency or ""] = curs.get(r.currency or "", 0) + abs(r.earn_micros)
    cur = max(curs, key=curs.get) if curs else ""

    def total(field, window, pid=None):
        vals = [getattr(cell[(q, d)], field) for q in ([pid] if pid else ids) for d in window
                if (q, d) in cell and getattr(cell[(q, d)], field) is not None
                and (field != "earn_micros" or (cell[(q, d)].currency or "") == cur)]
        return sum(vals) if vals else None

    def daily(field, pid=None):
        out = []
        for d in days:
            vals = [getattr(cell[(q, d)], field) for q in ([pid] if pid else ids)
                    if (q, d) in cell and getattr(cell[(q, d)], field) is not None
                    and (field != "earn_micros" or (cell[(q, d)].currency or "") == cur)]
            out.append(sum(vals) if vals else None)
        return out

    def tiles(pid=None):
        out = []
        for key, label, money in (("views", "Views", False), ("video_views", "Video views", False),
                                  ("engagements", "Engagements", False), ("follows", "New followers", False),
                                  ("earn_micros", "Earnings", True)):
            now, before = total(key, days, pid), total(key, prev, pid)
            delta = None
            if now is not None and before:
                delta = (now - before) / abs(before) * 100
            out.append({"label": label, "value": _fmt_money(now, cur) if money else _fmt_n(now), "missing": now is None,
                        "delta": None if delta is None else f"{'+' if delta >= 0 else '−'}{abs(delta):.0f}%",
                        "up": delta is not None and delta >= 0})
        return out

    health = db.health_all()
    page_rows = []
    for p in sorted(pages, key=lambda x: x.name.casefold()):
        h = health.get(p.id)
        v_now, v_before = total("views", days, p.id), total("views", prev, p.id)
        change = (v_now - v_before) / abs(v_before) * 100 if v_now is not None and v_before else None
        spark = _spark(daily("views", p.id))
        page_rows.append({"id": p.id, "name": p.name, "pic": p.picture, "health": _health(h),
                          "delta": None if change is None else ("+" if change >= 0 else MINUS) + f"{abs(change):.0f}%",
                          "up": change is not None and change >= 0, "video_views": _fmt_n(total("video_views", days, p.id)),
                          "spark_area": (spark.split(" ")[0].split(",")[0] + ",30 " + spark + " " + spark.split(" ")[-1].split(",")[0] + ",30")
                          if spark else "",
                          "views": _fmt_n(v_now), "engagements": _fmt_n(total("engagements", days, p.id)),
                          "follows": _fmt_n(total("follows", days, p.id)),
                          "earnings": _fmt_money(total("earn_micros", days, p.id), cur),
                          "spark": spark,
                          "note": (h.stats_note if h else "") or "", "earn_note": (h.earn_note if h else "") or "",
                          "short_note": ((h.stats_note if h else "") or "").split(". ")[0].rstrip(".") + "." if h and h.stats_note else "",
                          "checked": h.checked_at if h else None})
    return {"days": days, "cur": cur, "tiles": tiles, "total": total, "daily": daily, "page_rows": page_rows,
            "health": health, "first": days[0], "last": days[-1]}


def _switch(pages: list) -> list:
    """The Page switcher: every connected Page with its status, A to Z."""
    health = db.health_all()
    out = []
    for p in sorted(pages, key=lambda x: x.name.casefold()):
        hl = _health(health.get(p.id))
        out.append({"id": p.id, "name": p.name, "pic": p.picture, "cls": hl["cls"], "icon": hl["icon"], "label": hl["label"]})
    return out


def _range(request) -> int:
    try:
        r = int(request.query_params.get("range", "28"))
    except ValueError:
        r = 28
    return r if r in RANGES else 28


def _updated_text(at: str) -> str:
    try:
        mins = int((db.utcnow() - dt.datetime.fromisoformat(at)).total_seconds() // 60)
    except ValueError:
        return "not yet"
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins} min ago"
    if mins < 48 * 60:
        return f"{mins // 60} hr ago"
    return f"{mins // 1440} days ago"


async def analytics_page(request):
    if not authed(request):
        return redirect("/login")
    rng = _range(request)
    pages = _analytics_pages(await run(db.list_pages))
    await _refresh_if_old(pages)
    v = await run(_build_view, pages, rng)
    views = v["daily"]("views")
    earn = v["daily"]("earn_micros")
    return page(request, "analytics.html", rng=rng, ranges=RANGES, state=ANALYTICS, has_pages=bool(pages),
                switch=await run(_switch, pages), current=None,
                updated=_updated_text(await run(db.get_setting, "analytics_at", "")), tiles=v["tiles"](),
                views_chart=_chart(v["days"], views), earn_chart=_chart(v["days"], earn, True, v["cur"]),
                has_earn=any(x is not None for x in earn), rows=v["page_rows"], first=_day_label(v["first"], True),
                last=_day_label(v["last"], True), table_days=list(zip([_day_label(d, True) for d in v["days"]],
                                                                       [_fmt_n(x) for x in views],
                                                                       [_fmt_money(x, v["cur"]) for x in earn])))


async def analytics_one(request):
    if not authed(request):
        return redirect("/login")
    try:
        pid = int(request.path_params["pid"])
    except ValueError:
        return redirect("/analytics")
    p = await run(db.get_page, pid)
    if p is None or p.gone or not p.token_enc:
        flash(request, "That Page isn't connected anymore.")
        return redirect("/analytics")
    rng = _range(request)
    await _refresh_if_old([p])
    v = await run(_build_view, [p], rng)
    views, earn = v["daily"]("views", p.id), v["daily"]("earn_micros", p.id)
    reels = await run(db.reels_for, p.id)
    row = v["page_rows"][0]
    return page(request, "analytics_page.html", p=p, row=row, rng=rng, ranges=RANGES, state=ANALYTICS,
                switch=await run(_switch, _analytics_pages(await run(db.list_pages))), current=p.id,
                updated=_updated_text(await run(db.get_setting, "analytics_at", "")), tiles=v["tiles"](p.id),
                views_chart=_chart(v["days"], views), earn_chart=_chart(v["days"], earn, True, v["cur"]),
                has_earn=any(x is not None for x in earn), first=_day_label(v["first"], True), last=_day_label(v["last"], True),
                table_days=list(zip([_day_label(d, True) for d in v["days"]], [_fmt_n(x) for x in views],
                                    [_fmt_money(x, v["cur"]) for x in earn])),
                reels=[{"title": r.title or "(no caption)", "url": r.url, "when": local(r.created).strftime("%b %d, %Y") if r.created else "",
                        "plays": _fmt_n(r.plays), "reach": _fmt_n(r.reach), "avg": _fmt_ms(r.avg_ms), "likes": _fmt_n(r.likes),
                        "social": _fmt_n(r.social), "follows": _fmt_n(r.follows)} for r in reels])


async def analytics_refresh(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    _start_refresh(_analytics_pages(await run(db.list_pages)))
    back = str(form.get("back", "/analytics"))
    return redirect(back if back.startswith("/analytics") else "/analytics")


async def analytics_state(request):
    if not authed(request):
        return JSONResponse({"ok": False}, status_code=401)
    return JSONResponse({"ok": True, **ANALYTICS})


class SecurityHeaders:
    CSP = ("default-src 'self'; img-src 'self' data: blob: https://*.fbcdn.net https://*.fbsbx.com https://*.facebook.com; "
           "media-src 'self' blob:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
           "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self' https://www.facebook.com; "
           "object-src 'none'")

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                extra = {b"x-frame-options": b"DENY", b"x-content-type-options": b"nosniff",
                         b"referrer-policy": b"same-origin", b"content-security-policy": self.CSP.encode()}
                if os.getenv("RENDER"):
                    extra[b"strict-transport-security"] = b"max-age=31536000"
                have = {k.lower() for k, _ in headers}
                headers += [(k, v) for k, v in extra.items() if k not in have]
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app):
    await _startup()
    yield


async def _startup():
    await run(db.init)
    if not ADMIN_PASSWORD:
        log.warning("ADMIN_PASSWORD is not set: nobody can log in until you add it on Render.")
    if not os.getenv("SECRET_KEY"):
        log.warning("SECRET_KEY is not set: logins and saved Facebook keys stop working after every restart.")


routes = [
    Route("/", queue_page),
    Route("/login", login_page),
    Route("/login", login_submit, methods=["POST"]),
    Route("/logout", logout, methods=["POST"]),
    Route("/pages", pages_page),
    Route("/pages/save", page_save, methods=["POST"]),
    Route("/accounts/remove", account_remove, methods=["POST"]),
    Route("/fb/connect", fb_connect),
    Route("/fb/callback", fb_callback),
    Route("/api/upload", upload_init, methods=["POST"]),
    Route("/api/upload/{uid}", upload_chunk, methods=["PUT"]),
    Route("/api/upload/{uid}/thumb", upload_thumb, methods=["POST"]),
    Route("/api/upload/{uid}/done", upload_done, methods=["POST"]),
    Route("/api/posts", posts_api),
    Route("/posts/cancel", post_cancel, methods=["POST"]),
    Route("/posts/clear", clear_history, methods=["POST"]),
    Route("/analytics", analytics_page),
    Route("/analytics/page/{pid}", analytics_one),
    Route("/analytics/refresh", analytics_refresh, methods=["POST"]),
    Route("/api/analytics/state", analytics_state),
    Route("/health", health),
    Route("/privacy", privacy_page),
    Route("/data-deletion", deletion_page),
]

app = Starlette(
    routes=routes,
    lifespan=lifespan,
    middleware=[
        Middleware(SecurityHeaders),
        Middleware(SessionMiddleware, secret_key=SECRET_KEY, session_cookie="rs_session", max_age=30 * 24 * 3600,
                   same_site="lax", https_only=bool(os.getenv("RENDER"))),
    ],
)
