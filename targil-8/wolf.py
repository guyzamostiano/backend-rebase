"""The Wolf: keeps page_views_hourly lean by deleting buckets older than the retention window.

Runs forever. Each pass deletes in small batches (DELETE ... LIMIT n), one implicit
transaction per statement, so it never holds a long lock while the APIs keep writing.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import aiomysql

DATABASE_URL = os.environ.get("DATABASE_URL", "mysql://targil7:targil7@127.0.0.1:3306/users_service")
RETENTION_HOURS = int(os.environ.get("RETENTION_HOURS", "48"))
CLEAN_INTERVAL_SECONDS = int(os.environ.get("CLEAN_INTERVAL_SECONDS", "600"))
CLEAN_BATCH_SIZE = int(os.environ.get("CLEAN_BATCH_SIZE", "1000"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("wolf")

DELETE_SQL = "DELETE FROM page_views_hourly WHERE hour_start < %s LIMIT %s"

_db = urlparse(DATABASE_URL)


def connect() -> aiomysql.connection._ConnectionContextManager:
    return aiomysql.connect(
        host=_db.hostname,
        port=_db.port or 3306,
        user=_db.username,
        password=_db.password or "",
        db=_db.path.lstrip("/"),
        autocommit=True,
    )


async def clean_once() -> int:
    """Delete everything older than the retention window, in batches. Returns rows deleted."""
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=RETENTION_HOURS)
    total = 0
    async with connect() as conn, conn.cursor() as cur:
        while True:
            await cur.execute(DELETE_SQL, (cutoff, CLEAN_BATCH_SIZE))
            total += cur.rowcount
            if cur.rowcount < CLEAN_BATCH_SIZE:
                return total


async def main() -> None:
    log.info("wolf started: retention=%dh interval=%ds batch=%d", RETENTION_HOURS, CLEAN_INTERVAL_SECONDS, CLEAN_BATCH_SIZE)
    while True:
        try:
            deleted = await clean_once()
            log.info("cleaned %d rows", deleted)
        except Exception:
            log.exception("clean pass failed, will retry next interval")
        await asyncio.sleep(CLEAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
