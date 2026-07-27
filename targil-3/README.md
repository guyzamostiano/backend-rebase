# HTTP File Server

A FastAPI + uvicorn HTTP server that stores binary blobs on the local disk, together with a selected subset of the request headers.

## Features:
- **Upsert:** `POST` on an existing id overwrites the blob and its stored headers.
- **Header persistence:** `Content-Type` and any `x-rebase-*` header (case-insensitive) are stored alongside the blob and returned on `GET`.
- **Content-Type inference:** if no `Content-Type` was stored, it is guessed from the id via `mimetypes`, falling back to `application/octet-stream`.
- **Quota enforcement:** payload size, total disk usage, total blob count, header key/value lengths, header count and id format are all validated before anything is written.
- **Atomic writes:** files are staged as `.tmp` and `fsync`ed before being swapped into place, so a crash can never expose a half-written blob.

Blobs are written to `targil-3/storage/blobs/` as two files per blob: `{id}.bin` (payload) and `{id}.headers.json` (stored headers).

---

# How to run:

First, open your terminal and navigate into the project directory:
```bash
cd targil-3
```

Ensure you have `uv` installed on your machine.

### 1. Run the server
```bash
uv run main.py
```

The server listens on `http://0.0.0.0:8000`.

### 2. Verify it is up
```bash
curl http://localhost:8000/
# {"status":"ok"}
```

Interactive API docs (Swagger UI) are available at http://localhost:8000/docs.

---

# API

## `POST /blobs/{id}`
Create or overwrite a blob. The request body is the raw binary payload.

```bash
curl -X POST http://localhost:8000/blobs/hello.txt \
  -H "Content-Type: text/plain" \
  -H "x-rebase-author: guy" \
  --data-binary "hello world"

# {"id":"hello.txt","size":11}
```

**Stored headers:** only `Content-Type` and headers starting with `x-rebase-` are persisted. All other request headers are ignored.

**Errors (400):**

| Error | Condition |
|-------|-----------|
| `missing Content-Length header` | request sent without `Content-Length` (e.g. chunked transfer) |
| `invalid Content-Length header` | `Content-Length` is not a non-negative integer |
| `Content-Length does not match payload` | declared length differs from the received body |
| `payload length exceeds MAX_PAYLOAD_LENGTH` | payload larger than 10MB |
| `disk space exceeds MAX_DISK_QUOTA` | storing the blob would push total disk usage past 1GB — payloads **and** stored-header files both count (the current version of an overwritten blob is excluded from the calculation) |
| `total number of blobs exceeds MAX_BLOBS_TOTAL` | new blob would exceed 1,000,000 blobs |
| `header key exceeds MAX_HEADER_KEY_LENGTH` | a stored header key is longer than 30 chars |
| `header value exceeds MAX_HEADER_VALUE_LENGTH` | a stored header value is longer than 400 chars |
| `count(stored-headers) exceeds MAX_HEADER_COUNT` | more than 20 stored headers |
| `id contains invalid characters` | id has characters outside `a-z A-Z 0-9 . _ -` |
| `id exceeds MAX_ID_LENGTH` | id longer than 200 chars |

## `GET /blobs/{id}`
Return the blob payload with its stored headers.

```bash
curl -i http://localhost:8000/blobs/hello.txt

# HTTP/1.1 200 OK
# content-type: text/plain
# x-rebase-author: guy
# content-length: 11
#
# hello world
```


If no `Content-Type` was stored, it is inferred from the id — `GET /blobs/notes.txt` returns `text/plain`, while an id with no recognizable extension returns `application/octet-stream`.

**Errors:** `404` — `blob not found`.

## `DELETE /blobs/{id}`
Delete a blob and its stored headers.

```bash
curl -X DELETE http://localhost:8000/blobs/hello.txt
# 204 No Content
```

Deleting a non-existent blob also returns `204` — no `404` by design.

---

# Limits

| Constant | Value |
|----------|-------|
| `MAX_PAYLOAD_LENGTH` | 10MB |
| `MAX_DISK_QUOTA` | 1GB |
| `MAX_HEADER_KEY_LENGTH` | 30 |
| `MAX_HEADER_VALUE_LENGTH` | 400 |
| `MAX_HEADER_COUNT` | 20 |
| `MAX_ID_LENGTH` | 200 |
| `MAX_BLOBS_TOTAL` | 1,000,000 |

All limits are defined at the top of `main.py`.
