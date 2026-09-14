import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import urlparse

import aiomysql
import uvicorn
from fastapi import FastAPI, Query, Response, status
from pydantic import BaseModel

DATABASE_URL = os.environ.get("DATABASE_URL", "mysql://targil7:targil7@127.0.0.1:3306/users_service")

HOURS_IN_REPORT = 24
MULTI_HOUR_FORMAT = "%Y-%m-%d_%H:%M"  # e.g. "2025-06-01_21:00"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("page_views")


# --------------------------------------------------------------------------- time helpers

def to_utc_naive(dt: datetime) -> datetime:
    """Normalize to naive UTC. Naive input is assumed to already be UTC."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def floor_to_hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def parse_iso(value: str) -> datetime:
    return to_utc_naive(datetime.fromisoformat(value))


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------- db

_db = urlparse(DATABASE_URL)


def connect() -> aiomysql.connection._ConnectionContextManager:
    """One connection per request, no pool. autocommit=True: every statement is its own
    implicit transaction, which is the only kind of transaction this service uses."""
    return aiomysql.connect(
        host=_db.hostname,
        port=_db.port or 3306,
        user=_db.username,
        password=_db.password or "",
        db=_db.path.lstrip("/"),
        autocommit=True,
    )


# Multi-row INSERT + ON DUPLICATE KEY UPDATE: one round trip, one atomic statement.
# `AS new` is the MySQL 8.0.19+ row alias, replacing the deprecated VALUES(views).
INCREMENT_SQL_HEAD = "INSERT INTO page_views_hourly (page, hour_start, views) VALUES "
INCREMENT_SQL_TAIL = " AS new ON DUPLICATE KEY UPDATE views = page_views_hourly.views + new.views"

REPORT_SQL = """
    SELECT hour_start, views
    FROM page_views_hourly
    WHERE page = %s AND hour_start >= %s AND hour_start < %s
"""


async def increment(rows: list[tuple[str, datetime, int]]) -> None:
    """rows: (page, hour_start, views) triples. Must be non-empty."""
    placeholders = ", ".join(["(%s, %s, %s)"] * len(rows))
    params = [value for row in rows for value in row]
    async with connect() as conn, conn.cursor() as cur:
        await cur.execute(INCREMENT_SQL_HEAD + placeholders + INCREMENT_SQL_TAIL, params)


# --------------------------------------------------------------------------- api

class SingleView(BaseModel):
    page: str
    timestamp: str  # ISO-8601


class HourlyViews(BaseModel):
    h: int
    v: int


class Report(BaseModel):
    data: list[HourlyViews]


app = FastAPI()


@app.post("/page-views/single/", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def page_view_single(payload: SingleView) -> Response:
    hour_start = floor_to_hour(parse_iso(payload.timestamp))
    await increment([(payload.page, hour_start, 1)])
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/page-views/multi/", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def page_view_multi(payload: dict[str, dict[str, int]]) -> Response:
    """payload: {page: {"YYYY-MM-DD_HH:MM": views, ...}, ...}"""
    rows = [
        (page, floor_to_hour(datetime.strptime(hour, MULTI_HOUR_FORMAT)), views)
        for page, hours in payload.items()
        for hour, views in hours.items()
        if views > 0
    ]
    if rows:
        await increment(rows)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/report/{page}", response_model=Report)
async def report(
    page: str,
    now: str | None = Query(default=None, description="ISO-8601; defaults to current UTC time"),
    order: Literal["asc", "desc"] = "asc",
    take: int = Query(default=HOURS_IN_REPORT, ge=1, le=HOURS_IN_REPORT),
) -> Report:
    """The 24 complete hours before the current hour, oldest first by default.

    E.g. now=21:40 -> buckets 21:00 yesterday .. 20:00 today (the running 21:00 hour is excluded).
    Hours with no views are reported as 0, so the full report always has 24 entries.
    """
    window_end = floor_to_hour(parse_iso(now) if now else utc_now())
    window_start = window_end - timedelta(hours=HOURS_IN_REPORT)

    async with connect() as conn, conn.cursor() as cur:
        await cur.execute(REPORT_SQL, (page, window_start, window_end))
        views_by_hour = {hour_start: views for hour_start, views in await cur.fetchall()}

    buckets = [window_start + timedelta(hours=i) for i in range(HOURS_IN_REPORT)]
    data = [HourlyViews(h=bucket.hour, v=views_by_hour.get(bucket, 0)) for bucket in buckets]
    if order == "desc":
        data.reverse()
    return Report(data=data[:take])


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
