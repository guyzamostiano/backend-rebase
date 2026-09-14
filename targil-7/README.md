# Users Microservice

A small users service on top of MySQL 8, built with FastAPI and `aiomysql`. One table, three endpoints, structured logs shipped to logz.io.

# How to run

Requires a running MySQL 8 with a database and user for the service. One-time bootstrap as root:

```sql
CREATE DATABASE users_service CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE USER 'targil7'@'127.0.0.1' IDENTIFIED BY 'targil7';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, INDEX, ALTER ON users_service.* TO 'targil7'@'127.0.0.1';
```

Then, from `targil-7/`:

```bash
mysql -u targil7 -ptargil7 -h 127.0.0.1 users_service < schema.sql
cp .env.example .env            # fill in LOGZIO_TOKEN
uv run --env-file .env main.py
```

The service listens on `0.0.0.0:8000`. Interactive docs at `http://127.0.0.1:8000/docs`.

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `mysql://targil7:targil7@127.0.0.1:3306/users_service` | MySQL connection string |
| `LOGZIO_TOKEN` | empty | logz.io shipping token. When empty, logs go to the console only |
| `LOGZIO_URL` | `https://listener-eu.logz.io:8071` | logz.io listener for the account's region |

# API

### `POST /users/`

Upsert by email. Body: `{"email": "...", "full_name": "..."}`. The service generates the id and `joined_at` (UTC). The response body is always empty (CQRS: commands don't return the entity).

| Outcome | Status | Log event |
|---|---|---|
| user created | `201 Created`, with `Location: /users/{email}` | `user_created` |
| user was soft-deleted and is now active again | `200 OK` | `user_reactivated` |
| user already active, nothing changed | `204 No Content` | `user_already_active` |

Reactivation clears `deleted_since` only. The original `joined_at` and `full_name` are kept.

### `GET /users/{email}`

`200` with `{"email", "full_name", "joined_at"}` where `joined_at` is ISO-8601 in UTC (e.g. `2026-09-08T13:41:38.414252Z`). `404` if the user doesn't exist or is soft-deleted.

### `DELETE /users/{email}`

Soft delete: sets `deleted_since = utc-now` only if it was `NULL`. Always `204` with an empty body, since the intent is "make sure this user is gone".

| Outcome | Log event |
|---|---|
| row updated | `user_soft_deleted` |
| no active row for that email | `user_not_found_or_inactive` |

# How to test

```bash
curl -i -X POST http://127.0.0.1:8000/users/ -H 'content-type: application/json' \
     -d '{"email":"alice@example.com","full_name":"Alice"}'        # 201
curl -i -X POST http://127.0.0.1:8000/users/ -H 'content-type: application/json' \
     -d '{"email":"alice@example.com","full_name":"Alice"}'        # 204
curl -i http://127.0.0.1:8000/users/alice@example.com               # 200 + payload
curl -i -X DELETE http://127.0.0.1:8000/users/alice@example.com     # 204
curl -i http://127.0.0.1:8000/users/alice@example.com               # 404
curl -i -X POST http://127.0.0.1:8000/users/ -H 'content-type: application/json' \
     -d '{"email":"alice@example.com","full_name":"Alice"}'        # 200 (reactivated)
```

# Design notes

### The single-statement upsert

```sql
INSERT INTO users (id, email, full_name, joined_at) VALUES (?, ?, ?, ?)
ON DUPLICATE KEY UPDATE deleted_since = NULL
```

MySQL reports *affected* rows for this statement, which encodes all three outcomes:

| affected rows | meaning |
|---|---|
| 1 | inserted |
| 2 | existing row matched and changed (`deleted_since` went from a value to `NULL`) |
| 0 | existing row matched, nothing changed (`deleted_since` was already `NULL`) |

This relies on the client **not** setting `CLIENT_FOUND_ROWS`, which would make MySQL report *matched* rows instead. `aiomysql` / PyMySQL leave it off by default.

Updating `full_name` in the `ON DUPLICATE KEY` clause would break the trick: an active user changing their name would also produce 2 affected rows and be logged as "reactivated". Hence only `deleted_since` is touched.

### Why `BINARY(16)` for the id

Following [Storing UUID Values in MySQL Tables](https://dev.mysql.com/blog-archive/storing-uuid-values-in-mysql-tables/):

- A UUID is 128 bits. As text (`CHAR(36)`) it costs 36 bytes; as `BINARY(16)` it costs 16. InnoDB copies the primary key into every secondary index, so the saving is paid once per index.
- Random UUIDs (v4) insert into random positions of the clustered index, fragmenting it. The article's fix is to move the timestamp bits of a v1 UUID to the front. **UUIDv7** has the timestamp in the high 48 bits by design, so ids are generated in increasing order and inserts append to the end of the index, like `AUTO_INCREMENT` does. Python 3.11 has no `uuid.uuid7()`, so `main.py` builds one by hand (a few lines).
- Readability is preserved with a virtual column: `id_text CHAR(36) GENERATED ALWAYS AS (BIN_TO_UUID(id)) VIRTUAL`. It is computed on read and takes no disk space, so `SELECT id_text, email FROM users` stays human friendly.

Alternatives considered:

- `CHAR(36)`: what the spec literally says; simpler to debug, larger indexes, no ordering with v4.
- `AUTO_INCREMENT`: smallest and fastest, but the id can only be minted by the one database, which is the constraint a microservice is supposed to avoid.
- Snowflake ID (the stretch goal): a 64-bit `time | machine id | sequence` integer. Same ordering benefit as UUIDv7 in half the bytes, at the cost of assigning a unique machine id per instance.

### Timestamps

MySQL `DATETIME` has no timezone, so the service stores naive UTC and re-attaches UTC when serializing, producing the trailing `Z`.

### Logging

Stdlib `logging` with two handlers: a JSON-lines console formatter and the official `logzio-python-handler`, which ships records to logz.io in batches from a background thread. Every event carries a stable `event` field plus `email` (and `user_id` on creation), so they can be filtered in Kibana with e.g. `event:user_reactivated`.
