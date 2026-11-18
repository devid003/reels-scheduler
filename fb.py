"""Small Facebook Graph API client: connect accounts, list Pages, upload and schedule Reels."""
import asyncio
import os

import aiohttp

VERSION = os.getenv("FB_API_VERSION", "").strip().strip("/")       # blank = your app's own version
GRAPH = os.getenv("FB_GRAPH_URL", "https://graph.facebook.com").rstrip("/") + (f"/{VERSION}" if VERSION else "")
DIALOG = os.getenv("FB_DIALOG_URL", "https://www.facebook.com").rstrip("/") + (f"/{VERSION}" if VERSION else "") + "/dialog/oauth"
SCOPES = "pages_show_list,pages_read_engagement,pages_manage_posts"


class FBError(Exception):
    def __init__(self, message: str, code: int = 0, subcode: int = 0, status: int = 0):
        super().__init__(message)
        self.message, self.code, self.subcode, self.status = message, code, subcode, status

    @property
    def token_bad(self) -> bool:
        return self.code == 190 or self.status == 401


def app_id() -> str:
    return os.getenv("FB_APP_ID", "").strip()


def app_secret() -> str:
    return os.getenv("FB_APP_SECRET", "").strip()


def dialog_url(redirect_uri: str, state: str) -> str:
    from urllib.parse import urlencode
    q = {"client_id": app_id(), "redirect_uri": redirect_uri, "state": state, "response_type": "code"}
    cfg = os.getenv("FB_CONFIG_ID", "").strip()
    if cfg:
        q["config_id"] = cfg
    else:
        q["scope"] = SCOPES
    return DIALOG + "?" + urlencode(q)


async def _call(method: str, path: str, params: dict | None = None, data=None, headers=None, url: str | None = None,
                timeout: float = 60) -> dict:
    t = aiohttp.ClientTimeout(total=timeout)
    try:
        async with aiohttp.ClientSession(timeout=t) as s:
            async with s.request(method, url or f"{GRAPH}/{path.lstrip('/')}", params=params, data=data,
                                 headers=headers) as r:
                try:
                    body = await r.json(content_type=None)
                except Exception:
                    body = {"error": {"message": (await r.text())[:300] or f"HTTP {r.status}"}}
                if r.status >= 400 or (isinstance(body, dict) and "error" in body):
                    e = (body or {}).get("error") or {}
                    if not isinstance(e, dict):
                        e = {"message": str(e)}
                    raise FBError(str(e.get("error_user_msg") or e.get("message") or f"HTTP {r.status}")[:400],
                                  int(e.get("code") or 0), int(e.get("error_subcode") or 0), r.status)
                return body if isinstance(body, dict) else {"data": body}
    except asyncio.TimeoutError:
        raise FBError("Facebook took too long to answer.")
    except aiohttp.ClientError as e:
        raise FBError(f"Could not reach Facebook ({type(e).__name__}).")


# ---------- connecting an account ----------
async def exchange_code(code: str, redirect_uri: str) -> str:
    r = await _call("GET", "oauth/access_token", {"client_id": app_id(), "client_secret": app_secret(),
                                                  "redirect_uri": redirect_uri, "code": code})
    return r["access_token"]


async def long_lived(token: str) -> str:
    r = await _call("GET", "oauth/access_token", {"grant_type": "fb_exchange_token", "client_id": app_id(),
                                                  "client_secret": app_secret(), "fb_exchange_token": token})
    return r.get("access_token") or token


async def me(token: str) -> dict:
    return await _call("GET", "me", {"fields": "id,name", "access_token": token})


async def pages(user_token: str) -> list:
    """Pages this account can post to: [{id, name, access_token, picture}]."""
    out, url, params = [], None, {"fields": "id,name,access_token,tasks,picture{url}", "limit": "100",
                                  "access_token": user_token}
    for _ in range(20):
        r = await _call("GET", "me/accounts", params if url is None else None, url=url)
        for p in r.get("data", []):
            tasks = p.get("tasks") or []
            if p.get("access_token") and (not tasks or "CREATE_CONTENT" in tasks or "MANAGE" in tasks):
                out.append({"id": str(p["id"]), "name": p.get("name", ""), "access_token": p["access_token"],
                            "picture": ((p.get("picture") or {}).get("data") or {}).get("url", "")})
        url = (r.get("paging") or {}).get("next")
        if not url:
            break
    return out


# ---------- Reels ----------
async def reel_start(page_id: str, token: str) -> tuple:
    r = await _call("POST", f"{page_id}/video_reels", data={"upload_phase": "start", "access_token": token})
    return str(r["video_id"]), r.get("upload_url") or f"https://rupload.facebook.com/video-upload/{VERSION or 'v25.0'}/{r['video_id']}"


async def reel_upload(upload_url: str, token: str, path: str, size: int) -> None:
    with open(path, "rb") as f:
        r = await _call("POST", "", data=f, url=upload_url, timeout=1800,
                        headers={"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size),
                                 "Content-Type": "application/octet-stream"})
    if r.get("success") is False:
        raise FBError("Facebook did not accept the video file.")


async def reel_finish(page_id: str, token: str, video_id: str, description: str, when_ts: int | None, ai: bool) -> dict:
    data = {"upload_phase": "finish", "video_id": video_id, "description": description,
            "is_ai_generated": "true" if ai else "false", "access_token": token}
    if when_ts:
        data["video_state"], data["scheduled_publish_time"] = "SCHEDULED", str(when_ts)
    else:
        data["video_state"] = "PUBLISHED"
    r = await _call("POST", f"{page_id}/video_reels", data=data)
    if r.get("success") is False:
        raise FBError(str(r.get("message") or "Facebook did not accept the Reel."))
    return r


async def video_status(video_id: str, token: str) -> dict:
    r = await _call("GET", video_id, {"fields": "status,permalink_url", "access_token": token})
    return r


async def delete_video(video_id: str, token: str) -> None:
    await _call("DELETE", video_id, {"access_token": token})
