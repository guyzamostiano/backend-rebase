# HTTP Forward Proxy

A generic HTTP forward proxy supporting only `GET` requests, built on raw `asyncio` (no web framework). It parses the incoming request, forwards it to the destination with `httpx`, and relays the response back to the client.

- Every request header is forwarded, except: `connection`, `keep-alive`, `proxy-authenticate`, `proxy-authorization`, `te`, `trailer`, `transfer-encoding`, `upgrade`.
- Response status, headers and body are relayed as-is; `Content-Length` is recomputed and `Connection: close` is set by the proxy.
- Responses are fully consumed (≤10MB), not streamed.

# How to run

```bash
cd targil-4
uv run main.py
```

The proxy listens on `127.0.0.1:43210`.

# How to test

```bash
curl "http://httpbin.org/uuid" -x 127.0.0.1:43210
# {"uuid": "8aed500d-a4c9-450a-b03f-503ce11b6be5"}
```

# Errors

| Case | Response |
|------|----------|
| non-`GET` method | `405 Method Not Allowed` |
| malformed request / too many header lines | `400 Bad Request` |
| destination unreachable or timed out | `502 Bad Gateway` |
