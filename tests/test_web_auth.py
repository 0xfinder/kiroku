"""Exercise real OIDC cryptography and enumerate the HTTP authorization boundary."""

import time
from collections.abc import AsyncGenerator, Iterator
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, RouteContext, iter_route_contexts
from itsdangerous import BadSignature

from kiroku.web.app import create_app
from kiroku.web.auth import COOKIE, FLOW_COOKIE, SESSION_SECONDS, require_owner
from tests.fakes.oidc import FakeOIDC, config, provider_server
from tests.test_web import settings


@pytest.fixture
def oidc() -> Iterator[FakeOIDC]:
    with provider_server() as fake:
        yield fake


@pytest.fixture
async def auth_client(
    database_url: str, oidc: FakeOIDC
) -> AsyncGenerator[tuple[httpx.AsyncClient, FastAPI]]:
    app = create_app(settings(database_url), provider=oidc.provider)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)),
            base_url="https://testserver",
        ) as client,
    ):
        yield client, app


async def begin(client: httpx.AsyncClient) -> str:
    login = await client.get("/auth/login")
    assert login.status_code == 302
    assert "Secure" in login.headers["set-cookie"] and "HttpOnly" in login.headers["set-cookie"]
    async with httpx.AsyncClient() as provider:
        authorized = await provider.get(login.headers["location"])
    assert authorized.status_code == 302
    return authorized.headers["location"]


async def test_login_session_logout(auth_client: tuple[httpx.AsyncClient, FastAPI]) -> None:
    client, app = auth_client
    assert (await client.get("/api/me")).json() == {"owner": False}
    callback = await begin(client)
    response = await client.get(callback)
    assert response.status_code == 303, response.text
    cookie = response.headers["set-cookie"]
    assert all(
        flag in cookie
        for flag in ("Secure", "HttpOnly", "SameSite=lax", f"Max-Age={SESSION_SECONDS}")
    )
    assert app.state.auth.signer.loads(client.cookies[COOKIE]) == config().owner_email
    assert (await client.get("/api/me")).json() == {"owner": True, "email": config().owner_email}
    assert (
        await client.post(
            "/api/rules", json={"name": "new", "instructions": "Does he predict a winner?"}
        )
    ).status_code == 201
    assert (
        await client.post("/auth/logout", headers={"Origin": "https://evil.test"})
    ).status_code == 403
    assert (
        await client.post("/auth/logout", headers={"Origin": "https://testserver"})
    ).status_code == 303
    assert COOKIE not in client.cookies
    assert FLOW_COOKIE not in client.cookies
    assert (await client.get("/api/me")).json() == {"owner": False}
    assert (await client.get("/api/review")).status_code == 403


@pytest.mark.parametrize(
    "variant",
    [
        "forged",
        "expired",
        "audience",
        "issuer",
        "nonce",
        "missing_nonce",
        "unverified",
        "string_verified",
        "non_owner",
        "token_error",
    ],
)
async def test_reject_bad_identity(
    auth_client: tuple[httpx.AsyncClient, FastAPI], oidc: FakeOIDC, variant: str
) -> None:
    client, _ = auth_client
    callback = await begin(client)
    if variant == "forged":
        oidc.forge = True
    elif variant == "token_error":
        oidc.token_error = True
    else:
        oidc.claims = {
            "expired": {"exp": int(time.time()) - 60},
            "audience": {"aud": "other-client"},
            "issuer": {"iss": "https://evil.test"},
            "nonce": {"nonce": "other-nonce"},
            "missing_nonce": {"nonce": None},
            "unverified": {"email_verified": False},
            "string_verified": {"email_verified": "true"},
            "non_owner": {"email": "visitor@example.test"},
        }[variant]
    response = await client.get(callback)
    assert response.status_code == (403 if variant in {"unverified", "non_owner"} else 400), (
        response.text
    )
    assert COOKIE not in client.cookies
    assert FLOW_COOKIE not in client.cookies
    assert (await client.get("/api/me")).json() == {"owner": False}
    # An unsuccessful exchange clears the browser cookie too.
    assert (await client.get(callback)).status_code == 400
    assert oidc.exchanges == 1


async def test_state_browser_binding_expiry_and_single_use(
    auth_client: tuple[httpx.AsyncClient, FastAPI], oidc: FakeOIDC
) -> None:
    client, app = auth_client
    callback = await begin(client)
    state = parse_qs(urlsplit(callback).query)["state"][0]
    saved = client.cookies[FLOW_COOKIE]
    assert (await client.get(callback.replace(state, "wrong-state"))).status_code == 400
    assert FLOW_COOKIE not in client.cookies
    client.cookies.clear()
    assert (await client.get(callback)).status_code == 400
    client.cookies.set(FLOW_COOKIE, saved, domain="testserver.local", path="/auth")
    flow = app.state.auth.flow_signer.loads(saved)
    client.cookies.set(
        FLOW_COOKIE,
        app.state.auth.flow_signer.dumps(flow | {"expires": 0}),
        domain="testserver.local",
        path="/auth",
    )
    assert (await client.get(callback)).status_code == 400
    assert oidc.exchanges == 0
    callback = await begin(client)
    saved = client.cookies[FLOW_COOKIE]
    assert (await client.get(callback)).status_code == 303
    client.cookies.set(FLOW_COOKIE, saved, domain="testserver.local", path="/auth")
    assert (await client.get(callback)).status_code == 400
    assert oidc.exchanges == 2


@pytest.mark.parametrize("oidc_enabled", [False, True])
async def test_public_cannot_write_or_read_private(database_url: str, oidc_enabled: bool) -> None:
    app = create_app(replace(settings(database_url), oidc=config() if oidc_enabled else None))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)),
            base_url="https://testserver",
        ) as client,
    ):
        assert (await client.get("/api/me")).json() == {"owner": False}
        assert (await client.get("/api/auth")).json() == {"login": oidc_enabled}
        if not oidc_enabled:
            assert (await client.get("/auth/login")).status_code == 503
        for method, path in protected_routes(app):
            response = await client.request(
                method,
                path.replace("{name}", "missing")
                .replace("{decision_id}", "1")
                .replace("{job_id}", "missing"),
                json={},
            )
            assert response.status_code == 403, (method, path, response.text)
        client.cookies.set(COOKIE, "forged")
        assert (await client.get("/api/me")).json() == {"owner": False}
        if oidc_enabled:
            client.cookies.set(COOKIE, app.state.auth.signer.dumps("visitor@example.test"))
            assert (await client.get("/api/me")).json() == {"owner": False}
        for path in ("/api/sessions", "/api/rules"):
            assert (await client.get(path)).status_code == 200


def has_owner(dependency: Dependant) -> bool:
    return dependency.call is require_owner or any(
        has_owner(child) for child in dependency.dependencies
    )


def api_routes(app: FastAPI) -> Iterator[RouteContext]:
    # FastAPI 0.143 includes routers lazily; app.routes alone misses their endpoints.
    for context in iter_route_contexts(app.routes):
        if isinstance(context.original_route, APIRoute):
            yield context


PUBLIC_READS = {
    "/api/me",
    "/api/auth",
    "/auth/login",
    "/auth/callback",
    "/api/sessions",
    "/api/sessions/{session_id}",
    "/api/sessions/{session_id}/transcript",
    "/api/sessions/{session_id}/transcript/cursor",
    "/api/sessions/{session_id}/transcript/stream",
    "/api/sessions/{session_id}/notes",
    "/api/sessions/{session_id}/alerts",
    "/api/sessions/{session_id}/passes",
    "/api/rules",
}


def protected_routes(app: FastAPI) -> Iterator[tuple[str, str]]:
    for route in api_routes(app):
        assert route.path is not None
        for method in route.methods or set():
            if method not in {"GET", "HEAD"} or route.path not in PUBLIC_READS:
                yield method, route.path


def assert_owner_boundary(app: FastAPI) -> None:
    protected = set(protected_routes(app))
    assert ("POST", "/auth/logout") in protected
    for route in api_routes(app):
        if any((method, route.path) in protected for method in route.methods or set()):
            assert has_owner(route.dependant), f"owner guard missing: {route.path}"


def test_all_routes_have_owner_guard(database_url: str) -> None:
    assert_owner_boundary(create_app(settings(database_url)))


def test_route_enumeration_catches_removed_guard(database_url: str) -> None:
    app = create_app(settings(database_url))
    route = next(
        route
        for route in app.routes
        if isinstance(route, APIRoute)
        and route.path == "/api/rules"
        and "POST" in (route.methods or set())
    )
    route.dependant.dependencies.clear()
    with pytest.raises(AssertionError, match="owner guard missing: /api/rules"):
        assert_owner_boundary(app)


def test_expired_signed_cookie(database_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app(settings(database_url))
    signer = app.state.auth.signer
    cookie = signer.dumps(config().owner_email)

    def future_timestamp(_: object) -> int:
        return int(time.time()) + SESSION_SECONDS + 1

    monkeypatch.setattr(
        "itsdangerous.timed.TimestampSigner.get_timestamp",
        future_timestamp,
    )
    from starlette.requests import Request

    request = Request({"type": "http", "headers": [(b"cookie", f"{COOKIE}={cookie}".encode())]})
    assert app.state.auth.email(request) is None


def test_route_enumeration_catches_new_nested_private_read(database_url: str) -> None:
    app = create_app(settings(database_url))
    router = APIRouter()

    @router.get("/review/missing-guard")
    async def missing_guard() -> None:
        pass

    app.include_router(router, prefix="/api")
    with pytest.raises(AssertionError, match="owner guard missing: /api/review/missing-guard"):
        assert_owner_boundary(app)


async def test_stateless_login_survives_restart_and_unbounded_visitors(
    auth_client: tuple[httpx.AsyncClient, FastAPI],
    oidc: FakeOIDC,
) -> None:
    from kiroku.web.auth import OwnerAuth

    client, app = auth_client
    callback = await begin(client)
    saved = client.cookies[FLOW_COOKIE]
    for n in range(130):
        assert (
            await client.get(
                "/auth/login",
                headers={"CF-Connecting-IP": f"2001:db8:{n:x}::1"},
            )
        ).status_code == 302
    # Repeated logins from the owner address cannot exhaust server flow state either.
    for _ in range(5):
        assert (await client.get("/auth/login")).status_code == 302
    restarted = FastAPI()
    restarted.state.auth = OwnerAuth(
        replace(settings("unused"), oidc=app.state.auth.config), oidc.provider
    )
    restarted.include_router(restarted.state.auth.routes())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=restarted), base_url="https://testserver"
    ) as fresh:
        fresh.cookies.set(FLOW_COOKIE, saved, domain="testserver.local", path="/auth")
        assert (await fresh.get(callback)).status_code == 303
        assert (await fresh.get("/api/me")).json()["owner"] is True


async def test_tampered_flow_and_distinct_cookie_salts(
    auth_client: tuple[httpx.AsyncClient, FastAPI],
    oidc: FakeOIDC,
) -> None:
    client, app = auth_client
    callback = await begin(client)
    saved = client.cookies[FLOW_COOKIE]
    with pytest.raises(BadSignature):
        app.state.auth.signer.loads(saved)
    client.cookies.set(COOKIE, saved, domain="testserver.local", path="/")
    assert (await client.get("/api/me")).json() == {"owner": False}
    client.cookies.set(FLOW_COOKIE, "tampered" + saved, domain="testserver.local", path="/auth")
    assert (await client.get(callback)).status_code == 400
    assert FLOW_COOKIE not in client.cookies
    assert oidc.exchanges == 0


@pytest.mark.parametrize(
    "issuer", ["https://accounts.google.com", "accounts.google.com", "https://evil.test"]
)
async def test_google_issuer_variants(
    auth_client: tuple[httpx.AsyncClient, FastAPI],
    oidc: FakeOIDC,
    issuer: str,
) -> None:
    client, app = auth_client
    app.state.auth.provider = replace(oidc.provider, issuer="https://accounts.google.com")
    oidc.claims = {"iss": issuer}
    assert (await client.get(await begin(client))).status_code == (
        400 if issuer == "https://evil.test" else 303
    )


async def test_normalized_owner_and_origin(database_url: str, oidc: FakeOIDC) -> None:
    from kiroku.config import Settings

    normalized = Settings.load(
        {
            "KIROKU_SECRETS_FILE": "/absent",
            "GOOGLE_CLIENT_ID": "test-client",
            "GOOGLE_CLIENT_SECRET": "test-secret",
            "KIROKU_SESSION_SECRET": "s" * 48,
            "KIROKU_OWNER_EMAIL": "Owner@Example.TEST",
            "KIROKU_PUBLIC_URL": "HTTPS://Kiroku.orulon.com:443/",
        }
    ).oidc
    assert normalized is not None
    assert normalized.owner_email == "owner@example.test"
    assert normalized.public_url == "https://kiroku.orulon.com"
    oidc.claims = {"email": "OWNER@example.TEST"}
    app = create_app(replace(settings(database_url), oidc=normalized), provider=oidc.provider)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=normalized.public_url,
        ) as client,
    ):
        assert (await client.get(await begin(client))).status_code == 303
        assert (await client.get("/api/me")).json() == {
            "owner": True,
            "email": normalized.owner_email,
        }
        assert (
            await client.post("/auth/logout", headers={"Origin": normalized.public_url})
        ).status_code == 303


def test_route_enumeration_catches_new_read_by_default(database_url: str) -> None:
    app = create_app(settings(database_url))

    @app.get("/api/owner-stats")
    async def missing_guard() -> None:
        pass

    with pytest.raises(AssertionError, match="owner guard missing: /api/owner-stats"):
        assert_owner_boundary(app)


async def test_flow_cookie_age_expiry(
    auth_client: tuple[httpx.AsyncClient, FastAPI],
    oidc: FakeOIDC,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiroku.web.auth import FLOW_SECONDS

    client, _ = auth_client
    callback = await begin(client)
    future = int(time.time()) + FLOW_SECONDS + 1

    def future_timestamp(_: object) -> int:
        return future

    monkeypatch.setattr("itsdangerous.timed.TimestampSigner.get_timestamp", future_timestamp)
    assert (await client.get(callback)).status_code == 400
    assert FLOW_COOKIE not in client.cookies
    assert oidc.exchanges == 0
