# Load Balancer

A load balancer in front of the blob servers of exercise 3, built on raw `asyncio`.

- `POST` / `GET` / `DELETE` `/blobs/{id}` are proxied to a node, chosen by `md5(id) % len(nodes)` so every verb for an id reaches the same node.
- `POST /internal/nodes/` registers a node, but only during the first `REGISTRATION_DURATION_SECONDS`. `GET /internal/nodes/` lists them.
- Blobs are served only after the registration period ends.

# How to run

Start three blob servers:

```bash
cd targil-3
PORT=8000 STORAGE_DIR=./storage/node1 uv run main.py
PORT=8001 STORAGE_DIR=./storage/node2 uv run main.py
PORT=8002 STORAGE_DIR=./storage/node3 uv run main.py
```

Start the load balancer, listening on `0.0.0.0:8080`:

```bash
cd targil-5
uv run main.py
```

Register the nodes within 20 seconds:

```bash
for p in 8000 8001 8002; do
  curl -X POST http://localhost:8080/internal/nodes/ \
       -d "{\"destination\":{\"host\":\"localhost\",\"port\":$p},\"name\":\"node_$p\"}"
done
```

`destination.host` accepts only `a-z A-Z 0-9 _ -`, so use `localhost`, not `127.0.0.1`.

# How to test

```bash
curl -X POST http://localhost:8080/blobs/photo_1 --data-binary "hello"
curl http://localhost:8080/blobs/photo_1
curl -X DELETE http://localhost:8080/blobs/photo_1

curl http://localhost:8080/internal/nodes/
ls ../targil-3/storage/node1 ../targil-3/storage/node2 ../targil-3/storage/node3
```

# Configuration

| Variable | Default |
|---|---|
| `REGISTRATION_DURATION_SECONDS` | `20` |
| `LB_HOST` / `LB_PORT` | `0.0.0.0` / `8080` |
| `LOG_LEVEL` | `INFO` |
| `LOG_OWNER` | `Guy Zamostiano` |
| `LOGZIO_TOKEN` | *(unset — logs go to stdout only)* |
| `LOGZIO_HOST` / `LOGZIO_PORT` / `LOGZIO_TYPE` | `listener.logz.io` / `8071` / `targil-5-lb` |

Values may go in a `.env` file (`cp .env.example .env`); real environment variables take precedence.

Logs are one JSON object per line on stdout. To also ship them to Logz.io, supply your own token — set `LOGZIO_TOKEN`, plus `LOGZIO_HOST` for your account's region and `LOG_OWNER` to your name.

# Errors

| Case | Response |
|---|---|
| invalid registration payload | `400 Bad Request` |
| registration after the window closed | `410 Gone` |
| blob request before the window closed, or no nodes registered | `503 Service Unavailable` |
| verb other than `POST`/`GET`/`DELETE` on `/blobs/` | `405 Method Not Allowed` |
| node unreachable or timed out | `502 Bad Gateway` |
| malformed request | `400 Bad Request` |
| unknown path | `404 Not Found` |

Blob-level errors (unknown id, quota exceeded, bad id format) come from the node and are relayed unchanged.
