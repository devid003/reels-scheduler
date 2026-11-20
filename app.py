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
