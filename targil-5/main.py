import asyncio
import atexit
import hashlib
import http.client
import json
import logging
import os
import queue
import re
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone


def load_env_file(path: str = ".env") -> None:
    try:
        with open(path, encoding="utf-8") as env_file:
            lines = env_file.readlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_env_file()

HOST = os.getenv("LB_HOST", "0.0.0.0")
PORT = int(os.getenv("LB_PORT", "8080"))
REGISTRATION_DURATION_SECONDS = int(os.getenv("REGISTRATION_DURATION_SECONDS", "20"))
LOG_OWNER = os.getenv("LOG_OWNER", "Guy Zamostiano")

MAX_HEADER_LINES = 100  # Defend from untrusted client
MAX_BODY_BYTES = 64 * 1024 * 1024  # Memory guard only; the node enforces the real payload quota
UPSTREAM_TIMEOUT_SECONDS = 30

INTERNAL_NODES_PATHS = {"/internal/nodes", "/internal/nodes/"}
BLOBS_PREFIX = "/blobs/"
BLOB_METHODS = {"POST", "GET", "DELETE"}

NON_FORWARDED_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
REQUEST_HEADERS_NOT_FORWARDED = NON_FORWARDED_HEADERS | {"host", "content-length"}
RESPONSE_HEADERS_NOT_RELAYED = NON_FORWARDED_HEADERS | {"content-length"}

MAX_TEXT_LENGTH = 50
MAX_PORT = 65535

TEXT_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")



class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "severity": record.levelname,
            "message": record.getMessage(),
            "Owner": LOG_OWNER,
        }
        payload["@timestamp"] = payload["timestamp"]
        payload.update(getattr(record, "context", {}))
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def ctx(**fields) -> dict:
    return {"context": fields}


class LogzioHandler(logging.Handler):

    def __init__(self, token: str, host: str, port: int, log_type: str, batch_size: int = 50,
                 flush_interval: float = 3.0) -> None:
        super().__init__()
        self._path = f"/?token={token}&type={log_type}"
        self._host = host
        self._port = port
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._queue: queue.Queue[str] = queue.Queue(maxsize=10_000)
        threading.Thread(target=self._run, name="logzio-shipper", daemon=True).start()
        atexit.register(self.flush)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._queue.put_nowait(self.format(record))
        except queue.Full:
            pass

    def _collect(self) -> list[str]:
        lines: list[str] = []
        deadline = time.monotonic() + self._flush_interval
        while len(lines) < self._batch_size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                lines.append(self._queue.get(timeout=remaining))
            except queue.Empty:
                break
        return lines

    def _run(self) -> None:
        while True:
            lines = self._collect()
            if lines:
                self._ship(lines)

    def _ship(self, lines: list[str]) -> None:
        body = ("\n".join(lines) + "\n").encode("utf-8")
        try:
            connection = http.client.HTTPSConnection(self._host, self._port, timeout=10)
            connection.request("POST", self._path, body, {"Content-Type": "application/json"})
            response = connection.getresponse()
            status = response.status
            response.read()
            connection.close()
            if status >= 400:
                print(f"logzio rejected a batch: HTTP {status}", file=sys.stderr)
        except Exception as e:
            print(f"logzio shipping failed: {type(e).__name__}: {e}", file=sys.stderr)

    def flush(self) -> None:
        lines = []
        while True:
            try:
                lines.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if lines:
            self._ship(lines)


def build_handlers() -> list[logging.Handler]:
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(JsonFormatter())
    handlers: list[logging.Handler] = [stdout_handler]

    token = os.getenv("LOGZIO_TOKEN")
    if not token:
        return handlers  # no token configured -> stdout only

    logzio_handler = LogzioHandler(
        token=token,
        host=os.getenv("LOGZIO_HOST", "listener.logz.io"),
        port=int(os.getenv("LOGZIO_PORT", "8071")),
        log_type=os.getenv("LOGZIO_TYPE", "targil-5-lb"),
    )
    logzio_handler.setFormatter(JsonFormatter())
    handlers.append(logzio_handler)
    return handlers


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), handlers=build_handlers())
logger = logging.getLogger("lb")


def build_message(start_line: str, headers: list[tuple[str, str]], body: bytes) -> bytes:
    lines = [start_line]
    lines += [f"{key}: {value}" for key, value in headers]
    lines += [f"Content-Length: {len(body)}", "Connection: close"]
    head = "\r\n".join(lines) + "\r\n\r\n"
    return head.encode() + body


def build_response(status_code: int, reason: str, headers: list[tuple[str, str]], body: bytes) -> bytes:
    return build_message(f"HTTP/1.1 {status_code} {reason}", headers, body)


def build_request(method: str, target: str, headers: list[tuple[str, str]], body: bytes) -> bytes:
    return build_message(f"{method} {target} HTTP/1.1", headers, body)


def json_response(status_code: int, reason: str, payload: dict) -> bytes:
    body = json.dumps(payload).encode()
    return build_response(status_code, reason, [("Content-Type", "application/json")], body)


def error_response(status_code: int, reason: str, message: str) -> bytes:
    return json_response(status_code, reason, {"errorMessage": message})


# --- HTTP request parsing ---------------------------------------------------
async def read_request_head(reader: asyncio.StreamReader) -> tuple[str, str, list[tuple[str, str]]]:
    """Read and parse the request line and headers, up to the blank line.

    Returns (method, target, headers). Unlike the forward proxy of targil-4, the
    target here is origin-form -- a path such as '/blobs/abc', possibly with a
    query string. The destination is ours to choose, not the client's.
    """
    request_line = (await reader.readline()).decode("latin-1").strip()
    parts = request_line.split(" ")
    if len(parts) != 3:
        raise ValueError(f"malformed request line: {request_line!r}")
    method, target, _http_version = parts
    return method, target, await read_headers(reader)


async def read_body(reader: asyncio.StreamReader, headers: list[tuple[str, str]]) -> bytes:

    lookup = {key.lower(): value for key, value in headers}
    if "chunked" in lookup.get("transfer-encoding", "").lower():
        return await read_chunked_body(reader)

    raw_length = lookup.get("content-length")
    if raw_length is None:
        return b""
    try:
        length = int(raw_length)
    except ValueError:
        raise ValueError(f"malformed Content-Length: {raw_length!r}")
    if length < 0 or length > MAX_BODY_BYTES:
        raise ValueError(f"Content-Length out of range: {length}")
    return await reader.readexactly(length)


async def read_headers(reader: asyncio.StreamReader) -> list[tuple[str, str]]:
    headers = []
    for _ in range(MAX_HEADER_LINES):
        line = (await reader.readline()).decode("latin-1").strip()
        if line == "":
            return headers
        key, _, value = line.partition(":")
        headers.append((key.strip(), value.strip()))
    raise ValueError("too many header lines")


async def read_chunked_body(reader: asyncio.StreamReader) -> bytes:
    chunks = []
    total = 0
    while True:
        size_line = (await reader.readline()).decode("latin-1").strip()
        try:
            size = int(size_line.split(";")[0], 16)  # ignore chunk extensions
        except ValueError:
            raise ValueError(f"malformed chunk size: {size_line!r}")
        if size == 0:
            while (await reader.readline()).strip():  # skip trailers
                pass
            return b"".join(chunks)
        total += size
        if total > MAX_BODY_BYTES:
            raise ValueError(f"chunked body exceeds {MAX_BODY_BYTES} bytes")
        chunks.append(await reader.readexactly(size))
        await reader.readexactly(2)  # trailing CRLF


async def read_response(reader: asyncio.StreamReader) -> tuple[int, str, list[tuple[str, str]], bytes]:
    status_line = (await reader.readline()).decode("latin-1").strip()
    parts = status_line.split(" ", 2)
    if len(parts) < 2 or not parts[1].isdigit():
        raise ValueError(f"malformed status line: {status_line!r}")
    status_code = int(parts[1])
    reason = parts[2] if len(parts) > 2 else ""

    headers = await read_headers(reader)
    header_lookup = {key.lower(): value for key, value in headers}

    if status_code < 200 or status_code in (204, 304):
        return status_code, reason, headers, b""

    if "content-length" in header_lookup:
        return status_code, reason, headers, await reader.readexactly(int(header_lookup["content-length"]))
    if "chunked" in header_lookup.get("transfer-encoding", "").lower():
        return status_code, reason, headers, await read_chunked_body(reader)
    return status_code, reason, headers, await reader.read()


# --- nodes ------------------------------------------------------------------
class ValidationError(Exception):
    """Invalid client input. Raised deep, caught once at the route level -> 400."""


@dataclass(frozen=True)
class Node:
    id: str
    host: str
    port: int
    name: str | None

    def to_json(self) -> dict:
        return {"id": self.id, "destination": {"host": self.host, "port": self.port}, "name": self.name}


def validate_text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    if len(value) > MAX_TEXT_LENGTH:
        raise ValidationError(f"{field} must be at most {MAX_TEXT_LENGTH} characters")
    if not TEXT_PATTERN.match(value):
        raise ValidationError(f"{field} may only contain a-z, A-Z, 0-9, underscore and minus")
    return value


def validate_port(value: object) -> int:
    if value is None:
        raise ValidationError("destination.port is required")
    # bool is a subclass of int in Python, so `True` would otherwise pass as a port.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError("destination.port must be an integer")
    if not 0 <= value <= MAX_PORT:
        raise ValidationError(f"destination.port must be between 0 and {MAX_PORT}")
    return value


def parse_registration(body: bytes) -> tuple[str, int, str | None]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValidationError(f"body must be UTF-8 encoded JSON: {e}")
    if not isinstance(payload, dict):
        raise ValidationError("payload must be a JSON object")

    destination = payload.get("destination")
    if destination is None:
        raise ValidationError("destination is required")
    if not isinstance(destination, dict):
        raise ValidationError("destination must be a JSON object")

    host = destination.get("host")
    if host is None:
        raise ValidationError("destination.host is required")
    host = validate_text(host, "destination.host")

    port = validate_port(destination.get("port"))

    # null and "" are both equivalent to a missing name.
    raw_name = payload.get("name")
    name = None if raw_name is None or raw_name == "" else validate_text(raw_name, "name")

    return host, port, name


class NodeRegistry:

    def __init__(self) -> None:
        self._nodes: dict[tuple[str, int], Node] = {}

    def upsert(self, host: str, port: int, name: str | None) -> tuple[Node, bool]:
        """Add or update a node. Returns (node, created)"""
        key = (host, port)
        existing = self._nodes.get(key)
        node = Node(id=existing.id if existing else str(uuid.uuid4()), host=host, port=port, name=name)
        self._nodes[key] = node
        return node, existing is None

    def all(self) -> list[Node]:
        return sorted(self._nodes.values(), key=lambda node: (node.host, node.port))

    def __len__(self) -> int:
        return len(self._nodes)


registry = NodeRegistry()

_registration_deadline: float | None = None


def open_registration_window() -> None:
    global _registration_deadline
    _registration_deadline = time.monotonic() + REGISTRATION_DURATION_SECONDS


def is_registration_open() -> bool:
    return _registration_deadline is not None and time.monotonic() < _registration_deadline


async def close_registration_window() -> None:
    await asyncio.sleep(REGISTRATION_DURATION_SECONDS)
    if len(registry) == 0:
        logger.critical(
            "registration period closed with no nodes registered, cannot serve blobs",
            extra=ctx(total_nodes=0),
        )
        return
    logger.info(
        "registration period closed, now serving blobs",
        extra=ctx(total_nodes=len(registry), nodes=[node.to_json() for node in registry.all()]),
    )


# --- internal API -----------------------------------------------------------
def handle_register(body: bytes) -> bytes:
    if not is_registration_open():
        logger.warning("rejected registration, window closed", extra=ctx(total_nodes=len(registry)))
        return error_response(410, "Gone", "the request was rejected because registration period is over")

    try:
        host, port, name = parse_registration(body)
    except ValidationError as e:
        logger.warning("rejected invalid registration", extra=ctx(reason=str(e)))
        return error_response(400, "Bad Request", str(e))

    node, created = registry.upsert(host, port, name)
    logger.info(
        "node registered" if created else "node updated",
        extra=ctx(node_id=node.id, host=host, port=port, name=name, total_nodes=len(registry)),
    )
    return json_response(200, "OK", {"id": node.id})


def handle_list_nodes() -> bytes:
    nodes = registry.all()
    logger.debug("listed nodes", extra=ctx(total_nodes=len(nodes)))
    return json_response(200, "OK", {"data": [node.to_json() for node in nodes]})


# --- load balancing ---------------------------------------------------------
def stable_hash(value: str) -> int:
    return int.from_bytes(hashlib.md5(value.encode()).digest(), "big")


def select_node(blob_id: str, nodes: list[Node]) -> Node:
    return nodes[stable_hash(blob_id) % len(nodes)]


async def forward_to_node(
    node: Node, method: str, target: str, headers: list[tuple[str, str]], body: bytes
) -> tuple[int, str, list[tuple[str, str]], bytes]:
    """Open a second, separate connection to the node and relay the request."""
    reader, writer = await asyncio.open_connection(node.host, node.port)
    try:
        forwarded = [(key, value) for key, value in headers if key.lower() not in REQUEST_HEADERS_NOT_FORWARDED]
        forwarded.append(("Host", f"{node.host}:{node.port}"))
        writer.write(build_request(method, target, forwarded, body))
        await writer.drain()
        return await read_response(reader)
    finally:
        writer.close()
        await writer.wait_closed()


async def handle_blob(method: str, target: str, path: str, headers: list[tuple[str, str]], body: bytes) -> bytes:
    if method not in BLOB_METHODS:
        return error_response(405, "Method Not Allowed", f"{method} is not supported on {path}")

    nodes = registry.all()
    if not nodes:
        logger.error("cannot route blob request, no nodes registered", extra=ctx(path=path))
        return error_response(503, "Service Unavailable", "no nodes are registered")

    blob_id = path[len(BLOBS_PREFIX):]
    node = select_node(blob_id, nodes)
    logger.debug(
        "selected node",
        extra=ctx(blob_id=blob_id, node_id=node.id, host=node.host, port=node.port, total_nodes=len(nodes)),
    )

    try:
        status_code, reason, response_headers, response_body = await asyncio.wait_for(
            forward_to_node(node, method, target, headers, body), UPSTREAM_TIMEOUT_SECONDS
        )
    except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, ValueError) as e:
        logger.error(
            "failed to reach node",
            extra=ctx(blob_id=blob_id, node_id=node.id, host=node.host, port=node.port, reason=str(e)),
        )
        return error_response(502, "Bad Gateway", "failed to reach the destination node")

    logger.info(
        "proxied blob request",
        extra=ctx(method=method, blob_id=blob_id, node_id=node.id, name=node.name, status=status_code),
    )
    relayed = [(key, value) for key, value in response_headers if key.lower() not in RESPONSE_HEADERS_NOT_RELAYED]
    return build_response(status_code, reason, relayed, response_body)


# --- routing ----------------------------------------------------------------
async def route(method: str, target: str, headers: list[tuple[str, str]], body: bytes) -> bytes:
    """Dispatch a request: the internal API is served here, everything else is proxied."""
    path, _, _query = target.partition("?")

    if path in INTERNAL_NODES_PATHS:
        if method == "POST":
            return handle_register(body)
        if method == "GET":
            return handle_list_nodes()
        return error_response(405, "Method Not Allowed", f"{method} is not supported on {path}")

    if path.startswith(BLOBS_PREFIX):
        if is_registration_open():
            logger.warning("rejected blob request during registration window", extra=ctx(path=path))
            return error_response(503, "Service Unavailable", "registration period is not over yet")
        return await handle_blob(method, target, path, headers, body)

    return error_response(404, "Not Found", f"unknown path: {path}")


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    peer = str(writer.get_extra_info("peername"))
    try:
        await handle_client_inner(reader, writer)
    except (ValueError, asyncio.IncompleteReadError) as e:
        logger.warning("rejected malformed request", extra=ctx(peer=peer, reason=str(e)))
        writer.write(error_response(400, "Bad Request", "malformed request"))
    except Exception:
        logger.exception("unhandled error while serving request", extra=ctx(peer=peer))
        writer.write(error_response(500, "Internal Server Error", "internal error"))
    finally:
        try:
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()


async def handle_client_inner(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    method, target, headers = await read_request_head(reader)
    body = await read_body(reader, headers)
    writer.write(await route(method, target, headers, body))


async def main() -> None:
    try:
        server = await asyncio.start_server(handle_client, HOST, PORT)
    except OSError as e:
        logger.critical("cannot bind to port", extra=ctx(host=HOST, port=PORT, error=str(e)))
        sys.exit(1)

    open_registration_window()
    asyncio.create_task(close_registration_window())
    logger.info(
        "load balancer started, registration window open",
        extra=ctx(host=HOST, port=PORT, registration_duration_seconds=REGISTRATION_DURATION_SECONDS),
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
