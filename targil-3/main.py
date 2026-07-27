import json
import mimetypes
import os
import re
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

STORAGE_DIR = Path(__file__).parent / "storage" / "blobs"
STORAGE_DIR.mkdir(parents=True, exist_ok=True)


def blob_path(blob_id: str) -> Path:
    return STORAGE_DIR / f"{blob_id}.bin"


def headers_path(blob_id: str) -> Path:
    return STORAGE_DIR / f"{blob_id}.headers.json"


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


def write_atomically(writes: list[tuple[Path, bytes]]) -> None:
    """Stage every file next to its target, then swap them all in.

    Each individual file appears either fully written or not at all, so a crash
    can never expose a partially written blob.
    """
    staged = []
    try:
        for path, data in writes:
            tmp_path = path.parent / f"{path.name}{TMP_SUFFIX}"
            with open(tmp_path, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            staged.append((tmp_path, path))

        for tmp_path, path in staged:
            os.replace(tmp_path, path)
    finally:
        for tmp_path, _ in staged:
            tmp_path.unlink(missing_ok=True)


def get_disk_usage(exclude_blob_id: str | None = None) -> int:
    """Bytes held under STORAGE_DIR, counting payloads and stored headers alike."""
    excluded = set()
    if exclude_blob_id is not None:
        excluded = {blob_path(exclude_blob_id).name, headers_path(exclude_blob_id).name}

    total = 0
    for file in STORAGE_DIR.iterdir():
        if file.name in excluded or not file.is_file():
            continue
        total += file.stat().st_size
    return total


def count_blobs() -> int:
    return sum(1 for _ in STORAGE_DIR.glob("*.bin"))


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

    is_new_blob = not blob_path(blob_id).exists()
    if is_new_blob and count_blobs() >= MAX_BLOBS_TOTAL:
        raise HTTPException(status_code=400, detail="total number of blobs exceeds MAX_BLOBS_TOTAL")

    stored_headers = extract_stored_headers(request)

    body = await request.body()

    if len(body) != content_length:
        raise HTTPException(status_code=400, detail="Content-Length does not match payload")

    if len(body) > MAX_PAYLOAD_LENGTH:
        raise HTTPException(status_code=400, detail="payload length exceeds MAX_PAYLOAD_LENGTH")

    headers_body = json.dumps(stored_headers).encode()

    projected_usage = get_disk_usage(exclude_blob_id=blob_id) + len(body) + len(headers_body)
    if projected_usage > MAX_DISK_QUOTA:
        raise HTTPException(status_code=400, detail="disk space exceeds MAX_DISK_QUOTA")

    write_atomically([
        (blob_path(blob_id), body),
        (headers_path(blob_id), headers_body),
    ])

    return {"id": blob_id, "size": len(body)}


@app.get("/blobs/{blob_id}")
def get_blob(blob_id: str):
    validate_id(blob_id)

    path = blob_path(blob_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="blob not found")

    body = path.read_bytes()

    stored_headers = {}
    h_path = headers_path(blob_id)
    if h_path.exists():
        stored_headers = json.loads(h_path.read_text())

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
    h_path = headers_path(blob_id)

    if path.exists():
        path.unlink()
    if h_path.exists():
        h_path.unlink()

    return Response(status_code=204)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
