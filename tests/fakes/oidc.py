"""Local OIDC provider: real RSA/JWKS, single-use codes and PKCE, never Google."""

import base64
import hashlib
import json
import secrets
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from joserfc import jwt
from joserfc.jwk import RSAKey

from kiroku.config import OIDC, Secret
from kiroku.web.auth import Provider

OWNER = "owner@example.test"


def config(public_url: str = "https://testserver") -> OIDC:
    return OIDC(Secret("test-client"), Secret("test-secret"), Secret("s" * 48), OWNER, public_url)


class FakeOIDC:
    def __init__(self) -> None:
        self.key = RSAKey.generate_key(2048, parameters={"kid": "fixture"})
        self.forged_key = RSAKey.generate_key(2048, parameters={"kid": "fixture"})
        self.claims: dict[str, Any] = {}
        self.codes: dict[str, dict[str, str]] = {}
        self.forge = False
        self.token_error = False
        self.exchanges = 0
        self.provider = Provider()

    def token(self, nonce: str) -> str:
        now = int(time.time())
        claims = {
            "iss": self.provider.issuer,
            "sub": "fixture-owner",
            "aud": "test-client",
            "exp": now + 300,
            "iat": now,
            "nonce": nonce,
            "email": OWNER,
            "email_verified": True,
        } | self.claims
        return jwt.encode(
            {"alg": "RS256", "kid": "fixture"}, claims, self.forged_key if self.forge else self.key
        )


@contextmanager
def provider_server() -> Generator[FakeOIDC]:
    fake = FakeOIDC()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def respond(self, status: int, body: dict[str, Any]) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            url = urlsplit(self.path)
            if url.path == "/jwks":
                self.respond(200, {"keys": [fake.key.as_dict(private=False)]})
                return
            if url.path != "/authorize":
                self.respond(404, {})
                return
            params = {k: v[0] for k, v in parse_qs(url.query).items()}
            if (
                params.get("code_challenge_method") != "S256"
                or params.get("client_id") != "test-client"
            ):
                self.respond(400, {"error": "invalid_request"})
                return
            code = secrets.token_urlsafe(24)
            fake.codes[code] = params
            self.send_response(302)
            self.send_header(
                "Location",
                params["redirect_uri"] + "?" + urlencode({"code": code, "state": params["state"]}),
            )
            self.end_headers()

        def do_POST(self) -> None:
            fake.exchanges += 1
            params = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
            code = fake.codes.pop(params.get("code", [""])[0], None)
            verifier = params.get("code_verifier", [""])[0]
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .rstrip(b"=")
                .decode()
            )
            expected_auth = "Basic " + base64.b64encode(b"test-client:test-secret").decode()
            if (
                fake.token_error
                or code is None
                or challenge != code["code_challenge"]
                or self.headers.get("Authorization") != expected_auth
            ):
                self.respond(400, {"error": "invalid_grant"})
                return
            self.respond(
                200,
                {
                    "access_token": "fixture-access",
                    "token_type": "Bearer",
                    "id_token": fake.token(code["nonce"]),
                },
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    origin = f"http://127.0.0.1:{server.server_port}"
    fake.provider = Provider(origin, origin + "/authorize", origin + "/token", origin + "/jwks")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield fake
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def tls_files(directory: Path) -> tuple[str, str]:
    """Generate disposable localhost TLS material; never use production credentials."""
    import datetime
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    return str(cert_path), str(key_path)
