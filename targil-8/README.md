# Page-Views Analytics (part 1)

Counts page views per page per round hour and reports the last 24 hours. FastAPI + `aiomysql` on MySQL 8, plus a small cleaner process ("The Wolf"). Only an RDBMS is used for storage; the only transactions are the implicit ones of single SQL statements.

# Table design

Designed before coding, as required. One table, pre-aggregated by hour:

```sql
CREATE TABLE page_views_hourly (
    page       VARCHAR(200)    NOT NULL,
    hour_start DATETIME        NOT NULL,   -- UTC, truncated to the round hour
    views      BIGINT UNSIGNED NOT NULL DEFAULT 0,
    PRIMARY KEY (page, hour_start),
    KEY idx_hour_start (hour_start)        -- for the cleaner
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```

- **Natural composite key** `(page, hour_start)`. Rows are only ever addressed by this pair, so a surrogate id would add nothing.
- **Increments are one atomic statement**, whether one row or many:

  ```sql
  INSERT INTO page_views_hourly (page, hour_start, views) VALUES (?, ?, ?), (?, ?, ?), ...
  AS new ON DUPLICATE KEY UPDATE views = page_views_hourly.views + new.views;
  ```

  MySQL serializes the `views + n` updates per row, so concurrent callers never lose counts, and there is no read-modify-write in application code. This is the multi-row INSERT + `ON DUPLICATE KEY UPDATE` recipe: one round trip, atomic, portable-ish. (`AS new` is the MySQL 8.0.19+ row alias that replaced `VALUES(views)`.)
- **UTC everywhere.** Timestamps are normalized and truncated to the hour in the application, keeping database-side logic to a minimum.
- **Alternative rejected:** an append-only `page_view_events` table with one row per view. No write contention, but reports aggregate millions of rows and the cleaner has far more to delete. Pre-aggregation costs one hot row per (page, hour) instead, which is fine at this scale.
- **Scale-up options, not implemented:** a `shard TINYINT` column (random 0..15) added to the key to spread a very hot page over 16 rows, summed on read; `PARTITION BY HASH(page)` once the table is in the hundreds of millions of rows, valid because every report filters by `page`; `LOAD DATA INFILE` instead of multi-row INSERT for very large batches.

# How to run

Uses the `users_service` database and `targil7` user created for exercise 7.

```bash
cd targil-8
mysql -u targil7 -ptargil7 -h 127.0.0.1 users_service < schema.sql
cp .env.example .env
uv run --env-file .env main.py          # APIs on 0.0.0.0:8000
uv run --env-file .env wolf.py          # cleaner, in a second terminal
```

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `mysql://targil7:targil7@127.0.0.1:3306/users_service` | MySQL connection string |
| `RETENTION_HOURS` | `48` | wolf: delete buckets older than this |
| `CLEAN_INTERVAL_SECONDS` | `600` | wolf: pause between passes |
| `CLEAN_BATCH_SIZE` | `1000` | wolf: rows per `DELETE ... LIMIT` |

# API

### `POST /page-views/single/`

`{"page": "altman.html", "timestamp": "2025-06-01T21:15:00Z"}`. ISO-8601; an offset is converted to UTC, a naive timestamp is taken as UTC. Adds 1 to that page's hour bucket. Returns `204`.

### `POST /page-views/multi/`

```json
{
  "altman.html": {"2025-06-01_21:00": 103, "2025-06-01_22:00": 200},
  "musk.html":   {"2025-06-01_21:00": 838}
}
```

Hour keys are `YYYY-MM-DD_HH:MM` in UTC. The whole payload becomes a single multi-row upsert. Returns `204`.

### `GET /report/{page}?now=&order=&take=`

Returns the 24 complete hours before the current hour, oldest first:

```json
{"data": [{"h": 21, "v": 103}, {"h": 22, "v": 200}, {"h": 23, "v": 405}, {"h": 0, "v": 0}, ...]}
```

With `now` at 21:40 the window is 21:00 yesterday through 20:00 today; the running 21:00 hour is excluded, matching the example in the assignment. Hours with no views are reported as `0`, so the full report always has 24 entries.

| Param | Default | Meaning |
|---|---|---|
| `now` | current UTC time | ISO-8601, injects "now" for testing |
| `order` | `asc` | `asc` (oldest first) or `desc` |
| `take` | `24` | 1..24, first k entries after ordering |

Invalid `order` or `take` returns `422`.

# The Wolf (`wolf.py`)

Loops forever: every `CLEAN_INTERVAL_SECONDS` it deletes buckets older than `RETENTION_HOURS` with repeated `DELETE ... WHERE hour_start < ? LIMIT 1000` until a batch comes back short. Small batches, each its own implicit transaction, so the APIs are never blocked by a long-running delete. 48 hours of retention leaves a day of margin over what the report needs.

# How to test

```bash
B=http://127.0.0.1:8000
curl -X POST $B/page-views/multi/ -H 'content-type: application/json' \
  -d '{"altman.html":{"2025-06-01_21:00":103,"2025-06-01_22:00":200,"2025-06-01_23:00":405}}'
curl -X POST $B/page-views/single/ -H 'content-type: application/json' \
  -d '{"page":"altman.html","timestamp":"2025-06-01T21:15:00Z"}'
curl "$B/report/altman.html?now=2025-06-02T21:40:00"                 # h21=104, h22=200, h23=405, rest 0
curl "$B/report/altman.html?now=2025-06-02T21:40:00&order=desc&take=3"

# concurrency: 200 parallel increments must all land
seq 200 | xargs -P 50 -I{} curl -s -X POST $B/page-views/single/ -H 'content-type: application/json' \
  -d '{"page":"musk.html","timestamp":"2025-06-01T22:05:00Z"}'
curl "$B/report/musk.html?now=2025-06-02T21:40:00&take=2"            # h22=200
```
