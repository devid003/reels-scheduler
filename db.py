"""Database for the Reels Scheduler: connected Facebook accounts, their Pages, and scheduled posts."""
import datetime as dt
import os

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, create_engine, func, inspect, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _url() -> str:
    url = os.getenv("DATABASE_URL", "").strip() or "sqlite:///scheduler.db"
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg2://" + url[len("postgresql://"):]
    return url


engine = create_engine(_url(), pool_pre_ping=True, pool_recycle=280, future=True)
Session = sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class Account(Base):
    """A Facebook account that was connected (it gives access to the Pages it manages)."""
    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fb_user_id: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    token_enc: Mapped[str] = mapped_column(Text, default="")
    connected_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    problem: Mapped[str] = mapped_column(String(300), default="")


class Page(Base):
    __tablename__ = "pages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fb_page_id: Mapped[str] = mapped_column(String(40), unique=True)
    account_id: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    picture: Mapped[str] = mapped_column(String(600), default="")
    token_enc: Mapped[str] = mapped_column(Text, default="")
    slots: Mapped[str] = mapped_column(String(300), default="08:00, 12:00, 19:00")
    tags: Mapped[str] = mapped_column(String(500), default="")
    active: Mapped[bool] = mapped_column(default=True)
    gone: Mapped[bool] = mapped_column(default=False)          # no longer returned by Facebook for any account


class Post(Base):
    """One Reel for one Page."""
    __tablename__ = "posts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    page_id: Mapped[int] = mapped_column(Integer, index=True)
    batch: Mapped[str] = mapped_column(String(40), index=True, default="")
    file_name: Mapped[str] = mapped_column(String(200), default="")
    caption: Mapped[str] = mapped_column(Text, default="")
    scheduled_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True, index=True)   # UTC; None = post now
    ai: Mapped[bool] = mapped_column(default=True)
    status: Mapped[str] = mapped_column(String(20), default="waiting", index=True)
    # waiting -> sending -> scheduled -> posted   (or failed / canceled)
    video_id: Mapped[str] = mapped_column(String(40), default="")
    thumb_name: Mapped[str] = mapped_column(String(200), default="")   # custom cover photo file, if any
    error: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    checked_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


def _ensure_column(table: str, col: str, ddl: str) -> None:
    """Add a column to an existing table if it's not there yet (safe, non-destructive)."""
    try:
        cols = {c["name"] for c in inspect(engine).get_columns(table)}
    except Exception:
        return
    if col not in cols:
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))


def init() -> None:
    Base.metadata.create_all(engine)
    _ensure_column("posts", "thumb_name", "VARCHAR(200) DEFAULT ''")


# ---------- settings ----------
def get_setting(key: str, default: str = "") -> str:
    with Session() as s:
        row = s.get(Setting, key)
        return row.value if row else default


def set_setting(key: str, value: str) -> None:
    with Session.begin() as s:
        row = s.get(Setting, key)
        if row:
            row.value = value
        else:
            s.add(Setting(key=key, value=value))


# ---------- accounts and pages ----------
def save_account(fb_user_id: str, name: str, token_enc: str, pages: list) -> tuple:
    """pages: [{fb_page_id, name, picture, token_enc}]. Returns (account, new_pages, updated_pages)."""
    with Session.begin() as s:
        acc = s.scalars(select(Account).where(Account.fb_user_id == fb_user_id)).first()
        if acc is None:
            acc = Account(fb_user_id=fb_user_id)
            s.add(acc)
        acc.name, acc.token_enc, acc.connected_at, acc.problem = name[:200], token_enc, utcnow(), ""
        s.flush()
        new = upd = 0
        for p in pages:
            row = s.scalars(select(Page).where(Page.fb_page_id == p["fb_page_id"])).first()
            if row is None:
                row = Page(fb_page_id=p["fb_page_id"])
                s.add(row)
                new += 1
            else:
                upd += 1
            row.account_id, row.name, row.picture, row.token_enc, row.gone = (
                acc.id, p["name"][:200], p.get("picture", "")[:600], p["token_enc"], False)
        return acc, new, upd


def list_accounts() -> list:
    with Session() as s:
        return list(s.scalars(select(Account).order_by(Account.name)))


def remove_account(acc_id: int) -> None:
    with Session.begin() as s:
        acc = s.get(Account, acc_id)
        if acc:
            for p in s.scalars(select(Page).where(Page.account_id == acc_id)):
                p.token_enc, p.gone = "", True
            s.delete(acc)


def set_account_problem(acc_id: int, msg: str) -> None:
    with Session.begin() as s:
        acc = s.get(Account, acc_id)
        if acc:
            acc.problem = msg[:300]


def list_pages(usable_only: bool = False) -> list:
    with Session() as s:
        q = select(Page).order_by(func.lower(Page.name))
        if usable_only:
            q = q.where(Page.active.is_(True), Page.gone.is_(False), Page.token_enc != "")
        return list(s.scalars(q))


def get_page(pid: int):
    with Session() as s:
        return s.get(Page, pid)


def update_page(pid: int, slots: str, tags: str, active: bool) -> None:
    with Session.begin() as s:
        p = s.get(Page, pid)
        if p:
            p.slots, p.tags, p.active = slots[:300], tags[:500], active


# ---------- posts ----------
def add_posts(rows: list) -> list:
    with Session.begin() as s:
        objs = [Post(**r) for r in rows]
        s.add_all(objs)
        s.flush()
        return [o.id for o in objs]


def get_posts(ids: list) -> list:
    if not ids:
        return []
    with Session() as s:
        return list(s.scalars(select(Post).where(Post.id.in_(ids))))


def update_post(pid: int, **kw) -> None:
    with Session.begin() as s:
        p = s.get(Post, pid)
        if p:
            for k, v in kw.items():
                setattr(p, k, v)


def taken_times(page_id: int) -> set:
    """Times already used on this Page by posts that are still coming."""
    with Session() as s:
        return set(s.scalars(select(Post.scheduled_at).where(
            Post.page_id == page_id, Post.scheduled_at.is_not(None),
            Post.status.in_(("waiting", "sending", "scheduled")))))


def sent_last_day(page_id: int) -> int:
    """Reels sent to Facebook for this Page in the last 24 hours, plus ones waiting to be sent."""
    since = utcnow() - dt.timedelta(hours=24)
    with Session() as s:
        sent = s.scalar(select(func.count()).select_from(Post).where(
            Post.page_id == page_id, Post.sent_at >= since)) or 0
        pending = s.scalar(select(func.count()).select_from(Post).where(
            Post.page_id == page_id, Post.status.in_(("waiting", "sending")))) or 0
        return sent + pending


def upcoming_posts() -> list:
    with Session() as s:
        return list(s.scalars(select(Post).where(Post.status.in_(("waiting", "sending", "scheduled")))
                              .order_by(Post.scheduled_at.is_(None).desc(), Post.scheduled_at, Post.id)))


def recent_posts(days: int = 7, limit: int = 60) -> list:
    since = utcnow() - dt.timedelta(days=days)
    with Session() as s:
        return list(s.scalars(select(Post).where(Post.status.in_(("posted", "failed", "canceled")),
                                                 Post.created_at >= since)
                              .order_by(Post.created_at.desc()).limit(limit)))


def posts_to_check(limit: int = 15) -> list:
    """Scheduled posts whose time has passed (or 'post now' ones): ask Facebook if they went live."""
    now = utcnow()
    with Session() as s:
        return list(s.scalars(select(Post).where(
            Post.status == "scheduled",
            (Post.scheduled_at.is_(None)) | (Post.scheduled_at <= now - dt.timedelta(minutes=2)),
            (Post.checked_at.is_(None)) | (Post.checked_at <= now - dt.timedelta(minutes=3)))
            .order_by(Post.scheduled_at).limit(limit)))


def stale_sending(minutes: int = 45) -> list:
    since = utcnow() - dt.timedelta(minutes=minutes)
    with Session() as s:
        return list(s.scalars(select(Post).where(Post.status.in_(("waiting", "sending")), Post.created_at < since)))


def clear_finished() -> int:
    from sqlalchemy import delete
    with Session.begin() as s:
        r = s.execute(delete(Post).where(Post.status.in_(("failed", "canceled", "posted"))))
        return r.rowcount or 0
