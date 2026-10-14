"""Google OIDC and bounded, signed owner sessions. No peer address grants access."""

import secrets
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any, Protocol, cast

import httpx
from authlib.common.errors import AuthlibBaseError  # pyright: ignore[reportMissingTypeStubs]
from authlib.integrations.httpx_client import AsyncOAuth2Client  # pyright: ignore[reportMissingTypeStubs]
from authlib.oidc.core import CodeIDToken  # pyright: ignore[reportMissingTypeStubs]
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet
from pydantic import BaseModel, StrictBool

from kiroku.config import Settings

COOKIE = "kiroku_session"
FLOW_COOKIE = "kiroku_login"
SESSION_SECONDS = 12 * 60 * 60
FLOW_SECONDS = 600


@dataclass(frozen=True)
class Provider:
    issuer: str = "https://accounts.google.com"
    authorize: str = "https://accounts.google.com/o/oauth2/v2/auth"
    token: str = "https://oauth2.googleapis.com/token"
    jwks: str = "https://www.googleapis.com/oauth2/v3/certs"


class Identity(BaseModel):
    email: str
    email_verified: StrictBool


class Flow(BaseModel):
    state: str
    nonce: str
    verifier: str
    expires: float


def require_owner(request: Request) -> str:
    auth: OwnerAuth = request.app.state.auth
    email = auth.email(request)
    if email is None:
        raise HTTPException(403, "owner login required")
    return email


class OAuthClient(Protocol):
    # Authlib ships no typing for its HTTPX mixin. Describe only the used boundary.
    def create_authorization_url(self, url: str, **kwargs: str) -> tuple[str, str]: ...
    async def fetch_token(self, url: str, **kwargs: str) -> dict[str, Any]: ...
    async def get(self, url: str) -> httpx.Response: ...
    async def aclose(self) -> None: ...


class OwnerAuth:
    def __init__(self, settings: Settings, provider: Provider) -> None:
        self.config = settings.oidc
        self.provider = provider
        self.signer = (
            URLSafeTimedSerializer(self.config.session_secret.reveal(), salt="kiroku-owner-session")
            if self.config
            else None
        )
        self.flow_signer = (
            URLSafeTimedSerializer(self.config.session_secret.reveal(), salt="kiroku-login-flow")
            if self.config
            else None
        )

    def email(self, request: Request) -> str | None:
        if self.signer is None or self.config is None:
            return None
        try:
            email = self.signer.loads(request.cookies.get(COOKIE, ""), max_age=SESSION_SECONDS)
        except BadSignature:
            return None
        return self.config.owner_email if email == self.config.owner_email else None

    def routes(self) -> APIRouter:
        router = APIRouter()

        @router.get("/api/me")
        async def me(request: Request) -> dict[str, bool | str]:
            email = self.email(request)
            return {"owner": True, "email": email} if email else {"owner": False}

        @router.get("/api/auth")
        async def available() -> dict[str, bool]:
            return {"login": self.config is not None}

        @router.get("/auth/login")
        async def login() -> RedirectResponse:
            if self.config is None or self.signer is None or self.flow_signer is None:
                raise HTTPException(503, "owner login is not configured")
            state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
            flow = Flow(
                state=state, nonce=nonce, verifier=verifier, expires=time.time() + FLOW_SECONDS
            )
            async with self.client() as client:
                url, _ = client.create_authorization_url(
                    self.provider.authorize, state=state, nonce=nonce, code_verifier=verifier
                )
            response = RedirectResponse(url, status_code=302)
            response.set_cookie(
                FLOW_COOKIE,
                self.flow_signer.dumps(flow.model_dump()),
                max_age=FLOW_SECONDS,
                secure=True,
                httponly=True,
                samesite="lax",
                path="/auth",
            )
            return response

        @router.get("/auth/callback", response_model=None)
        async def callback(request: Request) -> RedirectResponse | JSONResponse:
            try:
                response = await complete(request)
            except HTTPException as error:
                response = JSONResponse({"detail": error.detail}, status_code=error.status_code)
            response.delete_cookie(
                FLOW_COOKIE, path="/auth", secure=True, httponly=True, samesite="lax"
            )
            return response

        async def complete(request: Request) -> RedirectResponse:
            if self.config is None or self.signer is None or self.flow_signer is None:
                raise HTTPException(503, "owner login is not configured")
            try:
                flow = Flow.model_validate(
                    self.flow_signer.loads(
                        request.cookies.get(FLOW_COOKIE, ""), max_age=FLOW_SECONDS
                    )
                )
            except (BadSignature, ValueError):
                raise HTTPException(400, "invalid login state") from None
            if flow.state != request.query_params.get("state"):
                raise HTTPException(400, "invalid login state")
            if flow.expires <= time.time():
                raise HTTPException(400, "login expired")
            code = request.query_params.get("code")
            if not code:
                raise HTTPException(400, "login was not completed")
            try:
                async with self.client() as client:
                    token = await client.fetch_token(
                        self.provider.token, code=code, code_verifier=flow.verifier
                    )
                    keys = await client.get(self.provider.jwks)
                    keys.raise_for_status()
                decoded = jwt.decode(
                    token["id_token"], KeySet.import_key_set(keys.json()), algorithms=["RS256"]
                )
                claims = CodeIDToken(  # pyright: ignore[reportUnknownVariableType]
                    decoded.claims,
                    decoded.header,
                    {
                        "iss": {
                            "values": (
                                ["https://accounts.google.com", "accounts.google.com"]
                                if self.provider.issuer == "https://accounts.google.com"
                                else [self.provider.issuer]
                            )
                        },
                        "aud": {"value": self.config.client_id.reveal()},
                    },
                    {
                        "nonce": flow.nonce,
                        "client_id": self.config.client_id.reveal(),
                        "access_token": token["access_token"],
                    },
                )
                claims.validate(leeway=0)  # pyright: ignore[reportUnknownMemberType]
                identity = Identity.model_validate(decoded.claims)
            except (JoseError, AuthlibBaseError, httpx.HTTPError, ValueError, KeyError, TypeError):
                raise HTTPException(400, "invalid identity response") from None
            if not identity.email_verified or identity.email.lower() != self.config.owner_email:
                raise HTTPException(403, "this Google account is not the owner")
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(
                COOKIE,
                self.signer.dumps(self.config.owner_email),
                max_age=SESSION_SECONDS,
                secure=True,
                httponly=True,
                samesite="lax",
            )
            return response

        @router.post("/auth/logout")
        async def logout(_: Annotated[str, Depends(require_owner)]) -> RedirectResponse:
            response = RedirectResponse("/", status_code=303)
            response.delete_cookie(COOKIE, secure=True, httponly=True, samesite="lax")
            return response

        return router

    @asynccontextmanager
    async def client(self) -> AsyncGenerator[OAuthClient]:
        assert self.config is not None
        client = cast(
            OAuthClient,
            AsyncOAuth2Client(
                self.config.client_id.reveal(),
                self.config.client_secret.reveal(),
                scope="openid email",
                redirect_uri=f"{self.config.public_url}/auth/callback",
                code_challenge_method="S256",
                timeout=10,
            ),
        )
        try:
            yield client
        finally:
            await client.aclose()
