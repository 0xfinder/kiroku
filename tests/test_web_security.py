"""Public admission limits through ASGI, including held and cancelled streams."""

import asyncio
from pathlib import Path

import httpx
import pytest
from starlette.responses import PlainTextResponse
from starlette.types import Receive, Scope, Send

from kiroku.web.security import Limits, PublicBoundary


async def ok(scope: Scope, receive: Receive, send: Send) -> None:
    await PlainTextResponse("ok")(scope, receive, send)


@pytest.mark.parametrize("peer,trusted", [("127.0.0.1", True), ("::1", True), ("192.0.2.1", False)])
async def test_rate_limit_and_tunnel_ip(peer: str, trusted: bool, tmp_path: Path) -> None:
    boundary = PublicBoundary(ok, static=tmp_path, limits=Limits(requests=2))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=boundary, client=(peer, 1)), base_url="https://testserver"
    ) as client:
        for _ in range(2):
            assert (
                await client.get("/api/me", headers={"CF-Connecting-IP": "203.0.113.1"})
            ).status_code == 200
        response = await client.get("/api/me", headers={"CF-Connecting-IP": "203.0.113.1"})
        assert response.status_code == 429 and response.headers["retry-after"] == "60"
        assert response.headers["cache-control"] == "no-store"
        assert (
            await client.get("/api/me", headers={"CF-Connecting-IP": "203.0.113.2"})
        ).status_code == (200 if trusted else 429)
        assert (await client.get("/rules")).status_code == 200


async def test_rate_reset_bounded_clients_and_invalid_ip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 100.0
    monkeypatch.setattr("kiroku.web.security.time.monotonic", lambda: now)
    boundary = PublicBoundary(ok, static=tmp_path, limits=Limits(requests=1, clients=1))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=boundary, client=("127.0.0.1", 1)),
        base_url="https://testserver",
    ) as client:
        assert (
            await client.get("/api/me", headers={"CF-Connecting-IP": "invalid"})
        ).status_code == 200
        assert (await client.get("/api/me")).status_code == 429
        assert (
            await client.get("/api/me", headers={"CF-Connecting-IP": "203.0.113.1"})
        ).status_code == 200
        now += 61
        assert (
            await client.get("/api/me", headers={"CF-Connecting-IP": "203.0.113.1"})
        ).status_code == 200
        assert len(boundary.clients) == 1


@pytest.mark.parametrize(
    "ips",
    [
        ("203.0.113.1", "203.0.113.1", "203.0.113.2", "203.0.113.3"),
        ("2001:db8:1:2::1", "2001:db8:1:2::ffff", "2001:db8:1:3::1", "2001:db8:1:4::1"),
    ],
)
async def test_stream_caps_release_on_cancel_and_error(
    tmp_path: Path, ips: tuple[str, str, str, str]
) -> None:
    entered = asyncio.Queue[None]()
    release = asyncio.Event()

    async def held(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["path"].endswith("/transcript/stream") and scope["method"] == "GET":
            entered.put_nowait(None)
            await release.wait()
            raise RuntimeError("fixture disconnect")
        await ok(scope, receive, send)

    boundary = PublicBoundary(
        held, static=tmp_path, limits=Limits(streams=2, streams_per_ip=1, clients=2)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=boundary, client=("127.0.0.1", 1)),
        base_url="https://testserver",
    ) as client:
        path = "/api/sessions/1/transcript/stream"
        tasks: list[asyncio.Task[httpx.Response]] = []
        try:
            for ip in (ips[0], ips[2]):
                tasks.append(
                    asyncio.create_task(client.get(path, headers={"CF-Connecting-IP": ip}))
                )
                await asyncio.wait_for(entered.get(), 1)
            assert (await client.get(path, headers={"CF-Connecting-IP": ips[1]})).status_code == 429
            assert (await client.get(path, headers={"CF-Connecting-IP": ips[3]})).status_code == 503
            assert (await client.head(path)).status_code == 200
            tasks[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await tasks[0]
            tasks.append(
                asyncio.create_task(client.get(path, headers={"CF-Connecting-IP": ips[3]}))
            )
            await asyncio.wait_for(entered.get(), 1)
            release.set()
            results = await asyncio.gather(*tasks[1:], return_exceptions=True)
            assert all(isinstance(result, RuntimeError) for result in results)
            assert boundary.streams == {}
            assert boundary.stream_count == 0
        finally:
            release.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def test_security_headers_and_script_hash(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text('<script>console.log("shell")</script>')
    boundary = PublicBoundary(ok, static=tmp_path, limits=Limits())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=boundary), base_url="https://testserver"
    ) as client:
        response = await client.get("/")
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "same-origin"
        assert response.headers["permissions-policy"] == "camera=(), microphone=(), geolocation=()"
        policy = response.headers["content-security-policy"]
        assert "'sha256-" in policy and "script-src 'self'" in policy
        assert "'unsafe-inline'" not in policy.split("script-src")[1].split(";")[0]
        assert "frame-ancestors 'none'" in policy and "object-src 'none'" in policy
        for path in ("/api/me", "/auth/login"):
            assert (await client.get(path)).headers["cache-control"] == "no-store"
        assert (await client.get("/service-worker.js")).headers["cache-control"] == "no-cache"


async def test_ipv6_rotation_shares_rate_limit(tmp_path: Path) -> None:
    boundary = PublicBoundary(ok, static=tmp_path, limits=Limits(requests=1))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=boundary, client=("127.0.0.1", 1)),
        base_url="https://testserver",
    ) as client:
        for ip, status in [
            ("2001:db8:1:2::1", 200),
            ("2001:db8:1:2::ffff", 429),
            ("2001:db8:1:3::1", 200),
        ]:
            assert (
                await client.get("/api/me", headers={"CF-Connecting-IP": ip})
            ).status_code == status


async def test_full_client_table_evicts_least_recently_seen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 100.0
    monkeypatch.setattr("kiroku.web.security.time.monotonic", lambda: now)
    boundary = PublicBoundary(ok, static=tmp_path, limits=Limits(requests=2, clients=2))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=boundary, client=("127.0.0.1", 1)),
        base_url="https://testserver",
    ) as client:

        async def hit(ip: str) -> int:
            return (await client.get("/api/me", headers={"CF-Connecting-IP": ip})).status_code

        assert await hit("203.0.113.1") == 200
        now += 1
        assert await hit("203.0.113.2") == 200
        now += 1
        assert await hit("203.0.113.1") == 200
        assert await hit("203.0.113.3") == 200
        assert list(boundary.clients) == ["203.0.113.1", "203.0.113.3"]
        assert await hit("203.0.113.1") == 429
        assert await hit("203.0.113.2") == 200
        assert list(boundary.clients) == ["203.0.113.1", "203.0.113.2"]
        now += 61
        assert await hit("203.0.113.4") == 200
        assert list(boundary.clients) == ["203.0.113.2", "203.0.113.4"]
        assert await hit("203.0.113.4") == 200
        assert list(boundary.clients) == ["203.0.113.4"]
