"""Single-worker admission and response headers, including streaming lifetimes."""

import base64
import hashlib
import ipaddress
import re
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from pathlib import Path

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


@dataclass(frozen=True)
class Limits:
    requests: int = 240
    window_s: float = 60
    streams: int = 32
    streams_per_ip: int = 4
    clients: int = 4096


def client_ip(scope: Scope) -> str:
    peer = scope.get("client")
    host = peer[0] if peer else "unknown"
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if loopback:
        forwarded = Headers(scope=scope).get("cf-connecting-ip")
        if forwarded:
            try:
                return str(ipaddress.ip_address(forwarded))
            except ValueError:
                pass
    return host


def admission_key(scope: Scope) -> str:
    host = client_ip(scope)
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host
    if isinstance(address, ipaddress.IPv6Address):
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address)


@dataclass
class Client:
    requests: deque[float]
    last_seen: float


def csp(static: Path) -> str:
    hashes: set[str] = set()
    for html in static.glob("*.html"):
        for attributes, script in re.findall(
            r"<script\b([^>]*)>(.*?)</script>", html.read_text(), re.S
        ):
            if "src=" not in attributes:
                digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
                hashes.add(f"'sha256-{digest}'")
    scripts = " ".join(sorted(hashes))
    return (
        f"default-src 'self'; script-src 'self' {scripts}; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
        "worker-src 'self'; manifest-src 'self'; object-src 'none'; base-uri 'none'; "
        "frame-ancestors 'none'; form-action 'self'"
    )


class PublicBoundary:
    def __init__(self, app: ASGIApp, *, static: Path, limits: Limits) -> None:
        self.app = app
        self.limits = limits
        self.clients: OrderedDict[str, Client] = OrderedDict()
        self.streams: dict[str, int] = {}
        self.stream_count = 0
        self.headers = {
            "Content-Security-Policy": csp(static),
            "X-Frame-Options": "DENY",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "same-origin",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        }

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]

        async def headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                values = MutableHeaders(scope=message)
                values.update(self.headers)
                if path.startswith(("/api", "/auth")):
                    values["Cache-Control"] = "no-store"
                if path == "/service-worker.js":
                    values["Cache-Control"] = "no-cache"
            await send(message)

        ip = admission_key(scope)
        if path == "/api" or path.startswith("/api/"):
            now = time.monotonic()
            cutoff = now - self.limits.window_s
            # Expire one oldest entry per admission; never scan the client table.
            if self.clients and next(iter(self.clients.values())).last_seen <= cutoff:
                self.clients.popitem(last=False)
            if ip not in self.clients:
                if len(self.clients) >= self.limits.clients:
                    self.clients.popitem(last=False)
                self.clients[ip] = Client(deque(), now)
            client = self.clients[ip]
            client.last_seen = now
            self.clients.move_to_end(ip)
            queue = client.requests
            while queue and queue[0] <= now - self.limits.window_s:
                queue.popleft()
            if len(queue) >= self.limits.requests:
                await JSONResponse(
                    {"detail": "API rate limit reached; try again later"},
                    status_code=429,
                    headers={"Retry-After": str(max(1, int(self.limits.window_s)))},
                )(scope, receive, headers)
                return
            queue.append(now)
        streaming = (
            path.startswith("/api/sessions/")
            and path.endswith("/transcript/stream")
            and scope["method"] == "GET"
        )
        if streaming:
            if self.streams.get(ip, 0) >= self.limits.streams_per_ip:
                await JSONResponse({"detail": "too many streams from this IP"}, status_code=429)(
                    scope, receive, headers
                )
                return
            if self.stream_count >= self.limits.streams:
                await JSONResponse({"detail": "stream capacity reached"}, status_code=503)(
                    scope, receive, headers
                )
                return
            self.streams[ip] = self.streams.get(ip, 0) + 1
            self.stream_count += 1
        try:
            await self.app(scope, receive, headers)
        finally:
            if streaming:
                self.streams[ip] -= 1
                self.stream_count -= 1
                if not self.streams[ip]:
                    del self.streams[ip]
