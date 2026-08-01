import asyncio

import httpx

HOST = "127.0.0.1"
PORT = 43210

MAX_HEADER_LINES = 100 #Defend from untrusted client


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

# httpx decompresses the body, so content-encoding/content-length no longer
# match the bytes we relay; we drop them and set Content-Length ourselves.
RESPONSE_HEADERS_NOT_RELAYED = NON_FORWARDED_HEADERS | {"content-encoding", "content-length"}

http_client = httpx.AsyncClient(timeout=30)


def build_response(status_code: int, reason: str, headers: list[tuple[str, str]], body: bytes) -> bytes:
    lines = [f"HTTP/1.1 {status_code} {reason}"]
    lines += [f"{key}: {value}" for key, value in headers]
    lines += [f"Content-Length: {len(body)}", "Connection: close"]
    head = "\r\n".join(lines) + "\r\n\r\n"
    return head.encode() + body


def error_response(status_code: int, reason: str, message: str) -> bytes:
    return build_response(status_code, reason, [("Content-Type", "text/plain")], f"{message}\n".encode())


async def read_request_head(reader: asyncio.StreamReader) -> tuple[str, str, list[tuple[str, str]]]:
    """Read and parse the request line and headers, up to the blank line.

    Returns (method, target, headers). The target is the absolute URL, e.g.
    'http://httpbin.org/uuid'
    """
    request_line = (await reader.readline()).decode("latin-1").strip()
    parts = request_line.split(" ")
    if len(parts) != 3:
        raise ValueError(f"malformed request line: {request_line!r}")
    method, target, _http_version = parts

    headers = []
    for _ in range(MAX_HEADER_LINES):
        line = (await reader.readline()).decode("latin-1").strip()
        if line == "":  # blank line = end of headers
            return method, target, headers
        key, _, value = line.partition(":")
        headers.append((key.strip(), value.strip()))
    raise ValueError("too many header lines")


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        try:
            method, target, headers = await read_request_head(reader)
        except ValueError:
            writer.write(error_response(400, "Bad Request", "malformed request"))
            return

        if method != "GET":
            writer.write(error_response(405, "Method Not Allowed", "proxy supports only GET"))
            return

        forward_headers = [(key, value) for key, value in headers if key.lower() not in NON_FORWARDED_HEADERS]

        try:
            response = await http_client.get(target, headers=forward_headers)
        except httpx.HTTPError as e:
            writer.write(error_response(502, "Bad Gateway", f"failed to reach destination: {e}"))
            return

        relayed_headers = [
            (key, value)
            for key, value in response.headers.multi_items()
            if key.lower() not in RESPONSE_HEADERS_NOT_RELAYED
        ]
        writer.write(build_response(response.status_code, response.reason_phrase, relayed_headers, response.content))
    finally:
        await writer.drain()
        writer.close()
        await writer.wait_closed()


async def main() -> None:
    server = await asyncio.start_server(handle_client, HOST, PORT)
    print(f"proxy listening on {HOST}:{PORT}")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
