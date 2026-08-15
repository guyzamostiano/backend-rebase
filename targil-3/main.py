import http.client
import json
import logging
import mimetypes
import os
import re
import threading
import time
import uvicorn
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, Response

app = FastAPI()

MAX_PAYLOAD_LENGTH = 10 * 1024 * 1024        # 10MB
MAX_DISK_QUOTA = 1024 * 1024 * 1024          # 1GB
MAX_HEADER_KEY_LENGTH = 30
MAX_HEADER_VALUE_LENGTH = 400
MAX_HEADER_COUNT = 20
MAX_ID_LENGTH = 200
MAX_BLOBS_TOTAL = 1_000_000

ID_PATTERN = re.compile(r"^[a-zA-Z0-9._-]+$")

TMP_SUFFIX = ".tmp"
BLOB_SUFFIX = ".blob"

STORAGE_DIR = Path(os.getenv("STORAGE_DIR") or Path(__file__).parent / "storage" / "blobs")
STORAGE_DIR.mkdir(parents=True, exist_ok=True)

PORT = int(os.getenv("PORT", "8000"))

# When MASTER_NODE_ADDRESS is set, this server registers itself with the load
# balancer on startup instead of being registered by hand.
MASTER_NODE_ADDRESS = os.getenv("MASTER_NODE_ADDRESS")
NODE_HOST = os.getenv("NODE_HOST", "localhost")
NODE_NAME = os.getenv("NODE_NAME")

DEFAULT_MASTER_PORT = 8080
REGISTRATION_DEADLINE_SECONDS = 30
REGISTRATION_RETRY_INTERVAL_SECONDS = 1
REGISTRATION_ATTEMPT_TIMEOUT_SECONDS = 2

logger = logging.getLogger("blob-server")


def blob_path(blob_id: str) -> Path:
    return STORAGE_DIR / f"{blob_id}{BLOB_SUFFIX}"


class StorageStats:
    """Disk usage and blob count, kept in memory and updated incrementally.

    The disk is scanned exactly once, at startup.
    every write/delete adjusts the counters
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.total_bytes = 0
        self.blob_count = 0

    def scan(self) -> None:
        self.total_bytes = 0
        self.blob_count = 0
        for file in STORAGE_DIR.iterdir():
            if not file.is_file():
                continue
            if file.name.endswith(TMP_SUFFIX):
                file.unlink(missing_ok=True)
                continue
            self.total_bytes += file.stat().st_size
            self.blob_count += 1


stats = StorageStats()
stats.scan()


def validate_id(blob_id: str) -> None:
    if len(blob_id) > MAX_ID_LENGTH:
        raise HTTPException(status_code=400, detail="id exceeds MAX_ID_LENGTH")
    if not ID_PATTERN.match(blob_id):
        raise HTTPException(status_code=400, detail="id contains invalid characters")


def extract_stored_headers(request: Request) -> dict:
    stored_headers = {}
    for key, value in request.headers.items():
        lower_key = key.lower()
        if lower_key == "content-type" or lower_key.startswith("x-rebase-"):
            stored_headers[key] = value

    if len(stored_headers) > MAX_HEADER_COUNT:
        raise HTTPException(status_code=400, detail="count(stored-headers) exceeds MAX_HEADER_COUNT")

    for key, value in stored_headers.items():
        if len(key) > MAX_HEADER_KEY_LENGTH:
            raise HTTPException(status_code=400, detail="header key exceeds MAX_HEADER_KEY_LENGTH")
        if len(value) > MAX_HEADER_VALUE_LENGTH:
            raise HTTPException(status_code=400, detail="header value exceeds MAX_HEADER_VALUE_LENGTH")

    return stored_headers


def encode_blob(stored_headers: dict, payload: bytes) -> bytes:
    """Single-file blob format: one JSON line with the headers, then the payload."""
    return json.dumps(stored_headers).encode() + b"\n" + payload


def decode_blob(raw: bytes) -> tuple[dict, bytes]:
    header_line, _, payload = raw.partition(b"\n")
    return json.loads(header_line), payload


def write_atomically(path: Path, data: bytes) -> None:
    """Stage next to the target, then swap in with a single os.replace."""
    tmp_path = path.parent / f"{path.name}{TMP_SUFFIX}"
    try:
        with open(tmp_path, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


@app.get("/")
def health_check():
    return {"status": "ok"}


@app.post("/blobs/{blob_id}")
async def upsert_blob(blob_id: str, request: Request):
    validate_id(blob_id)

    content_length = request.headers.get("content-length")
    if content_length is None:
        raise HTTPException(status_code=400, detail="missing Content-Length header")

    try:
        content_length = int(content_length)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid Content-Length header")

    if content_length < 0:
        raise HTTPException(status_code=400, detail="invalid Content-Length header")

    if content_length > MAX_PAYLOAD_LENGTH:
        raise HTTPException(status_code=400, detail="payload length exceeds MAX_PAYLOAD_LENGTH")

    stored_headers = extract_stored_headers(request)

    body = await request.body()

    if len(body) != content_length:
        raise HTTPException(status_code=400, detail="Content-Length does not match payload")

    if len(body) > MAX_PAYLOAD_LENGTH:
        raise HTTPException(status_code=400, detail="payload length exceeds MAX_PAYLOAD_LENGTH")

    file_body = encode_blob(stored_headers, body)
    new_size = len(file_body)
    path = blob_path(blob_id)

    with stats.lock:
        old_size = path.stat().st_size if path.exists() else None
        is_new_blob = old_size is None

        if is_new_blob and stats.blob_count >= MAX_BLOBS_TOTAL:
            raise HTTPException(status_code=400, detail="total number of blobs exceeds MAX_BLOBS_TOTAL")

        if stats.total_bytes - (old_size or 0) + new_size > MAX_DISK_QUOTA:
            raise HTTPException(status_code=400, detail="disk space exceeds MAX_DISK_QUOTA")

        write_atomically(path, file_body)

        stats.total_bytes += new_size - (old_size or 0)
        if is_new_blob:
            stats.blob_count += 1

    return {"id": blob_id, "size": len(body)}


@app.get("/blobs/{blob_id}")
def get_blob(blob_id: str):
    validate_id(blob_id)

    path = blob_path(blob_id)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="blob not found")

    stored_headers, body = decode_blob(raw)

    response_headers = dict(stored_headers)

    content_type = None
    for key, value in response_headers.items():
        if key.lower() == "content-type":
            content_type = value
            break

    if content_type is None:
        guessed_type, _ = mimetypes.guess_type(blob_id)
        content_type = guessed_type or "application/octet-stream"

    return Response(content=body, media_type=content_type, headers=response_headers)


@app.delete("/blobs/{blob_id}")
def delete_blob(blob_id: str):
    validate_id(blob_id)

    path = blob_path(blob_id)

    with stats.lock:
        if path.exists():
            size = path.stat().st_size
            path.unlink()
            stats.total_bytes -= size
            stats.blob_count -= 1

    return Response(status_code=204)


def parse_master_address(address: str) -> tuple[str, int]:
    """Split "host:port"."""
    host, _, port = address.partition(":")
    return host, int(port) if port else DEFAULT_MASTER_PORT


def register_once(master_host: str, master_port: int) -> tuple[int, str]:
    payload = {"destination": {"host": NODE_HOST, "port": PORT}}
    if NODE_NAME:
        payload["name"] = NODE_NAME

    connection = http.client.HTTPConnection(master_host, master_port, timeout=REGISTRATION_ATTEMPT_TIMEOUT_SECONDS)
    try:
        connection.request("POST", "/internal/nodes/", json.dumps(payload).encode(),
                           {"Content-Type": "application/json"})
        response = connection.getresponse()
        return response.status, response.read().decode()
    finally:
        connection.close()


def register_with_master() -> None:
    """Register with the load balancer, retrying while it is not yet listening."""
    try:
        master_host, master_port = parse_master_address(MASTER_NODE_ADDRESS)
    except ValueError:
        logger.error("MASTER_NODE_ADDRESS is not a valid host:port: %r", MASTER_NODE_ADDRESS)
        return

    deadline = time.monotonic() + REGISTRATION_DEADLINE_SECONDS
    while True:
        try:
            status, body = register_once(master_host, master_port)
        except OSError as e:
            # Refused or timed out: the master is not up yet, so keep polling.
            if time.monotonic() >= deadline:
                logger.error("gave up registering with %s after %ss: %s",
                             MASTER_NODE_ADDRESS, REGISTRATION_DEADLINE_SECONDS, e)
                return
            time.sleep(REGISTRATION_RETRY_INTERVAL_SECONDS)
            continue
        if status == 200:
            logger.info("registered with master %s as %s:%s -> %s", MASTER_NODE_ADDRESS, NODE_HOST, PORT, body)
        else:
            logger.error("master rejected registration: HTTP %s %s", status, body)
        return


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if MASTER_NODE_ADDRESS:
        threading.Thread(target=register_with_master, name="master-registration", daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=PORT)
