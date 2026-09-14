import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import aiomysql
import uvicorn
from fastapi import FastAPI, HTTPException, Response, status
from pydantic import BaseModel

DATABASE_URL = os.environ.get("DATABASE_URL", "mysql://targil7:targil7@127.0.0.1:3306/users_service")
LOGZIO_TOKEN = os.environ.get("LOGZIO_TOKEN", "")
LOGZIO_URL = os.environ.get("LOGZIO_URL", "https://listener-eu.logz.io:8071")


# --------------------------------------------------------------------------- logging

class JsonFormatter(logging.Formatter):
    """Console fallback: one JSON object per line, with the same fields we ship to logz.io."""

    STANDARD_ATTRS = set(vars(logging.makeLogRecord({})))

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "@timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        entry.update({k: v for k, v in vars(record).items() if k not in self.STANDARD_ATTRS})
        return json.dumps(entry, default=str)


def build_logger() -> logging.Logger:
    logger = logging.getLogger("users_service")
    logger.setLevel(logging.INFO)

    console = logging.StreamHandler()
    console.setFormatter(JsonFormatter())
    logger.addHandler(console)

    if LOGZIO_TOKEN:
        from logzio.handler import LogzioHandler

        # backup_logs=False: on repeated shipping failures the handler would otherwise
        # append the dropped records to logzio-failures-<timestamp>.txt in the working directory.
        logger.addHandler(LogzioHandler(LOGZIO_TOKEN, logzio_type="users-service", url=LOGZIO_URL, backup_logs=False))
    else:
        logger.warning("LOGZIO_TOKEN is not set, logging to console only")
    return logger


log = build_logger()


# --------------------------------------------------------------------------- ids

def uuid7() -> uuid.UUID:
    """UUID version 7: 48-bit unix-millisecond timestamp, then 74 random bits.

    Time in the high bits means ids are generated in increasing order, so InnoDB
    appends to the end of the primary key index instead of splitting random pages.
    Python 3.11 has no uuid.uuid7(), hence the hand-rolled version.
    """
    unix_ms = time.time_ns() // 1_000_000
    rand_a = int.from_bytes(os.urandom(2), "big") & 0x0FFF   # 12 bits
    rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)  # 62 bits
    value = (unix_ms << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    return uuid.UUID(int=value)


def utc_now() -> datetime:
    """Naive UTC datetime. MySQL DATETIME has no timezone, so we store UTC by convention."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_iso_utc(naive_utc: datetime) -> str:
    return naive_utc.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------- db

_db = urlparse(DATABASE_URL)


def connect() -> aiomysql.connection._ConnectionContextManager:
    """One connection per request; no pool, as the assignment allows.

    aiomysql (PyMySQL under the hood) does not set CLIENT_FOUND_ROWS, so rowcount
    for INSERT ... ON DUPLICATE KEY UPDATE is the *affected* rows count, which the
    upsert below relies on.
    """
    return aiomysql.connect(
        host=_db.hostname,
        port=_db.port or 3306,
        user=_db.username,
        password=_db.password or "",
        db=_db.path.lstrip("/"),
        autocommit=True,
    )


UPSERT_USER_SQL = """
    INSERT INTO users (id, email, full_name, joined_at)
    VALUES (%s, %s, %s, %s)
    ON DUPLICATE KEY UPDATE deleted_since = NULL
"""
# rowcount semantics for the statement above (MySQL, without CLIENT_FOUND_ROWS):
#   1 -> new row inserted                       (created)
#   2 -> existing row updated, value changed     (reactivated: deleted_since was set, now NULL)
#   0 -> existing row matched, nothing changed   (already active: deleted_since was already NULL)
ROWCOUNT_CREATED = 1
ROWCOUNT_REACTIVATED = 2
ROWCOUNT_ALREADY_ACTIVE = 0

GET_USER_SQL = """
    SELECT email, full_name, joined_at
    FROM users
    WHERE email = %s AND deleted_since IS NULL
"""

SOFT_DELETE_USER_SQL = """
    UPDATE users
    SET deleted_since = %s
    WHERE email = %s AND deleted_since IS NULL
"""


# --------------------------------------------------------------------------- api

class UpsertUserRequest(BaseModel):
    email: str
    full_name: str


class UserResponse(BaseModel):
    email: str
    full_name: str
    joined_at: str


app = FastAPI()


@app.post("/users/", status_code=status.HTTP_201_CREATED, response_class=Response)
async def upsert_user(payload: UpsertUserRequest) -> Response:
    user_id = uuid7()
    now = utc_now()

    async with connect() as conn, conn.cursor() as cur:
        await cur.execute(UPSERT_USER_SQL, (user_id.bytes, payload.email, payload.full_name, now))
        affected = cur.rowcount

    fields = {"email": payload.email}
    if affected == ROWCOUNT_CREATED:
        log.info("user created", extra={"event": "user_created", "user_id": str(user_id), **fields})
        return Response(status_code=status.HTTP_201_CREATED, headers={"Location": f"/users/{payload.email}"})
    if affected == ROWCOUNT_REACTIVATED:
        log.info("user reactivated", extra={"event": "user_reactivated", **fields})
        return Response(status_code=status.HTTP_200_OK)
    if affected == ROWCOUNT_ALREADY_ACTIVE:
        log.info("user already active", extra={"event": "user_already_active", **fields})
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    log.error("unexpected upsert rowcount", extra={"event": "upsert_unexpected_rowcount", "rowcount": affected, **fields})
    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


@app.get("/users/{email}", response_model=UserResponse)
async def get_user(email: str) -> UserResponse:
    async with connect() as conn, conn.cursor() as cur:
        await cur.execute(GET_USER_SQL, (email,))
        row = await cur.fetchone()

    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    db_email, full_name, joined_at = row
    return UserResponse(email=db_email, full_name=full_name, joined_at=to_iso_utc(joined_at))


@app.delete("/users/{email}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def soft_delete_user(email: str) -> Response:
    async with connect() as conn, conn.cursor() as cur:
        await cur.execute(SOFT_DELETE_USER_SQL, (utc_now(), email))
        affected = cur.rowcount

    if affected == 1:
        log.info("user soft-deleted", extra={"event": "user_soft_deleted", "email": email})
    else:
        log.info("user does not exist or is inactive", extra={"event": "user_not_found_or_inactive", "email": email})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
