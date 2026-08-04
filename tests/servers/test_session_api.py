"""The session endpoints (task E8) — this service's own half of sign-in.

The identity token is verified for real here: a generated RSA key stands in for
the gateway's JWKS, so ``POST /login`` is exercised against a genuine RS256
token with a genuine audience check rather than against a stubbed verifier.
What is faked is the gateway's *status* contract, which is an HTTP call this
repository does not implement either side of.

Three properties are worth more than the rest, and each has its own case:

- **A suspended account does not get a seven-day cookie.** The identity token
  proves who someone is at the moment they authenticated and says nothing about
  whether they may still use anything.
- **An unreachable gateway mints nothing.** Fail-closed, per AGENTS.md rule 4:
  a status call that cannot be made is not a status call that said yes.
- **A 401 from ``/me`` is the ordinary answer**, not a server error. The web
  app's ``ProtectedRoute`` loops if it ever arrives as a 5xx.
- **``/login`` and ``/me`` answer the same thing about the same person.** They
  are one contract — ``SessionUserResponse`` — and the session login mints is
  exactly what ``/me`` reads, so anything login reports that the session does
  not carry is a field the two routes disagree about for seven days.
"""

from __future__ import annotations

import time
from typing import Any

import jwt
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cloud_agent import identity
from cloud_agent.api import session as session_router
from cloud_agent.api.deps import get_gateway_client
from cloud_agent.gateway import GatewayError

pytestmark = pytest.mark.asyncio

_USER = "user_1"
_SESSION_SECRET = "session-signing-secret-long-enough-for-hs256"


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _FakeSigningKey:
    def __init__(self, key) -> None:
        self.key = key


@pytest.fixture(autouse=True)
def _identity_env(monkeypatch, key):
    """Point identity verification at a key this test controls."""
    identity.reset_jwks_cache()
    monkeypatch.setenv(identity.ENV_SESSION_SECRET, _SESSION_SECRET)
    monkeypatch.setenv(identity.ENV_JWKS_URL, "https://gateway.example/v1/identity/jwks")
    monkeypatch.delenv(identity.ENV_ISSUER, raising=False)

    class _Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def get_signing_key_from_jwt(self, token: str):
            return _FakeSigningKey(key.public_key())

    monkeypatch.setattr(identity, "PyJWKClient", _Client)
    yield
    identity.reset_jwks_cache()


def _identity_token(key, *, sub: str = _USER, aud: str = identity.AUDIENCE, **extra) -> str:
    """Mint what the gateway would hand the callback page."""
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": sub,
        "aud": aud,
        "iat": now,
        "exp": now + 600,
        "email": "owner@example.com",
        **extra,
    }
    return jwt.encode(claims, key, algorithm="RS256")


class _FakeGateway:
    """The gateway's user-status contract, answered in-process."""

    def __init__(self, record: dict[str, Any] | None = None, error: bool = False) -> None:
        self.record = record if record is not None else {"exists": True, "active": True}
        self.error = error
        self.asked: list[str] = []

    async def user_status(self, user_id: str) -> dict[str, Any]:
        self.asked.append(user_id)
        if self.error:
            raise GatewayError("gateway unreachable: nope")
        return self.record


def _app(gateway: _FakeGateway) -> FastAPI:
    app = FastAPI()
    app.include_router(session_router.router)
    app.dependency_overrides[get_gateway_client] = lambda: gateway
    return app


@pytest_asyncio.fixture()
async def client_and_gateway():
    gateway = _FakeGateway({"exists": True, "active": True, "role": "internal"})
    transport = ASGITransport(app=_app(gateway))
    # https, because the cookie carries ``Secure`` and a client talking plain
    # http would silently drop it — which would make every case below pass for
    # the wrong reason, or fail for one.
    async with AsyncClient(transport=transport, base_url="https://test") as client:
        yield client, gateway


@pytest_asyncio.fixture()
async def client_and_gateway_named():
    """A gateway whose status record carries a display name.

    Hypothetical today — the contract has no such field — which is the point:
    the plumbing has to be right before the field exists, or it arrives and
    only one of the two routes reports it.
    """
    gateway = _FakeGateway(
        {"exists": True, "active": True, "role": "internal", "user_name": "Ada Lovelace"}
    )
    transport = ASGITransport(app=_app(gateway))
    async with AsyncClient(transport=transport, base_url="https://test") as client:
        yield client, gateway


async def test_login_sets_an_httponly_session_cookie(client_and_gateway, key):
    """The happy path, and the cookie's attributes are the security surface."""
    client, gateway = client_and_gateway

    response = await client.post("/v1/session/login", json={"identity_token": _identity_token(key)})

    assert response.status_code == 200
    assert response.json() == {
        "id": _USER,
        "email": "owner@example.com",
        "role": "internal",
        "user_name": None,
    }
    assert gateway.asked == [_USER]

    raw = response.headers["set-cookie"]
    assert raw.startswith(f"{identity.SESSION_COOKIE}=")
    lowered = raw.lower()
    assert "httponly" in lowered
    assert "samesite=lax" in lowered
    assert "secure" in lowered
    assert "path=/" in lowered

    # And what it carries is a session this service can verify, holding the
    # role the *gateway* reported rather than anything the token claimed.
    claims = identity.verify_session(client.cookies[identity.SESSION_COOKIE])
    assert claims["sub"] == _USER
    assert claims["role"] == "internal"


async def test_the_gateways_role_wins_over_the_tokens_claim(client_and_gateway, key):
    """A role in the identity token must not be able to promote its bearer."""
    client, _ = client_and_gateway

    response = await client.post(
        "/v1/session/login",
        json={"identity_token": _identity_token(key, role="admin")},
    )

    assert response.status_code == 200
    assert response.json()["role"] == "internal"


async def test_a_suspended_account_gets_no_cookie(key):
    """The whole reason login asks the gateway at all.

    The identity token is valid — it was minted when this person authenticated
    — and says nothing about the suspension that happened afterwards. Without
    this call a suspended account walks away with a seven-day credential.
    """
    gateway = _FakeGateway({"exists": True, "active": False, "role": "pro"})
    transport = ASGITransport(app=_app(gateway))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/session/login", json={"identity_token": _identity_token(key)}
        )

    assert response.status_code == 403
    assert "set-cookie" not in response.headers


async def test_an_unknown_user_gets_no_cookie(key):
    """``exists: false`` is a refusal, not a shrug."""
    gateway = _FakeGateway({"exists": False})
    transport = ASGITransport(app=_app(gateway))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/session/login", json={"identity_token": _identity_token(key)}
        )

    assert response.status_code == 403
    assert "set-cookie" not in response.headers


async def test_a_status_record_this_service_cannot_read_is_a_refusal(key):
    """An answer with no recognizable verdict must not be read as a yes."""
    gateway = _FakeGateway({"user_id": _USER, "plan": "whatever-comes-next"})
    transport = ASGITransport(app=_app(gateway))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/session/login", json={"identity_token": _identity_token(key)}
        )

    assert response.status_code == 403
    assert "set-cookie" not in response.headers


async def test_an_unreachable_gateway_mints_no_session(key):
    """Fail closed. An outage must not become a way in."""
    gateway = _FakeGateway(error=True)
    transport = ASGITransport(app=_app(gateway))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/session/login", json={"identity_token": _identity_token(key)}
        )

    assert response.status_code == 503
    assert "set-cookie" not in response.headers


async def test_a_token_for_another_audience_is_refused(client_and_gateway, key):
    """The audience check is what stops one consumer's token acting as ours."""
    client, gateway = client_and_gateway

    response = await client.post(
        "/v1/session/login",
        json={"identity_token": _identity_token(key, aud="some-other-service")},
    )

    assert response.status_code == 401
    assert gateway.asked == [], "a token that did not verify must not reach the gateway"


async def test_a_garbage_identity_token_is_a_401(client_and_gateway):
    """Not a 500: an unverifiable credential is an ordinary refusal."""
    client, _ = client_and_gateway

    response = await client.post("/v1/session/login", json={"identity_token": "not-a-jwt"})

    assert response.status_code == 401


async def test_me_returns_the_current_user(client_and_gateway, key):
    """The cookie set at login is the only credential ``/me`` needs."""
    client, _ = client_and_gateway
    await client.post("/v1/session/login", json={"identity_token": _identity_token(key)})

    response = await client.get("/v1/session/me")

    assert response.status_code == 200
    assert response.json()["id"] == _USER
    assert response.json()["role"] == "internal"


async def test_login_and_me_answer_the_same_thing(client_and_gateway, key):
    """One contract, one answer.

    ``/me`` can only report what the session carries, so login reporting
    anything it did not mint is a field the two routes disagree about for the
    life of the cookie. ``user_name`` was exactly that: login read it off the
    gateway's status record and ``/me`` answered ``null``, always.
    """
    client, _ = client_and_gateway

    login = await client.post("/v1/session/login", json={"identity_token": _identity_token(key)})
    me = await client.get("/v1/session/me")

    assert login.status_code == 200
    assert me.status_code == 200
    assert login.json() == me.json()


async def test_a_display_name_survives_into_me(client_and_gateway_named, key):
    """And when there *is* a name to report, both report it.

    Nothing on either gateway contract carries a display name today, so the
    agreement above would also hold if login had simply stopped reporting one.
    This is what makes the fix the other one: the name is minted into the
    session, so ``/me`` can prove it.
    """
    client, _ = client_and_gateway_named

    login = await client.post("/v1/session/login", json={"identity_token": _identity_token(key)})
    me = await client.get("/v1/session/me")

    assert login.json()["user_name"] == "Ada Lovelace"
    assert me.json()["user_name"] == "Ada Lovelace"


async def test_me_is_401_without_a_session(client_and_gateway):
    """ "Not signed in" is the ordinary answer, and must not be a 5xx."""
    client, _ = client_and_gateway

    response = await client.get("/v1/session/me")

    assert response.status_code == 401


async def test_me_is_401_with_a_forged_session(client_and_gateway):
    """A cookie signed with anything else is not a session."""
    client, _ = client_and_gateway
    forged = jwt.encode(
        {"sub": _USER, "role": "admin", "exp": int(time.time()) + 600},
        "some-other-secret-entirely-long-enough",
        algorithm="HS256",
    )
    client.cookies.set(identity.SESSION_COOKIE, forged)

    response = await client.get("/v1/session/me")

    assert response.status_code == 401


async def test_me_is_401_when_the_session_has_expired(client_and_gateway):
    """And says so, so a client knows to sign in rather than to retry."""
    client, _ = client_and_gateway
    expired = jwt.encode(
        {"sub": _USER, "role": "internal", "exp": int(time.time()) - 1},
        _SESSION_SECRET,
        algorithm="HS256",
    )
    client.cookies.set(identity.SESSION_COOKIE, expired)

    response = await client.get("/v1/session/me")

    assert response.status_code == 401
    assert response.json()["detail"].startswith("Token has expired")


async def test_logout_clears_the_cookie(client_and_gateway, key):
    """And works without one, because signing out must not need a session."""
    client, _ = client_and_gateway
    await client.post("/v1/session/login", json={"identity_token": _identity_token(key)})

    response = await client.post("/v1/session/logout")

    assert response.status_code == 200
    assert not client.cookies.get(identity.SESSION_COOKIE)
    assert (await client.get("/v1/session/me")).status_code == 401
    # Idempotent: a second logout with no session is still a 200.
    assert (await client.post("/v1/session/logout")).status_code == 200


async def test_a_deployment_that_cannot_verify_sessions_says_503(monkeypatch, key):
    """Unset secret is an operator problem, reported as one — never a pass."""
    gateway = _FakeGateway({"exists": True, "active": True, "role": "internal"})
    transport = ASGITransport(app=_app(gateway))
    monkeypatch.delenv(identity.ENV_SESSION_SECRET, raising=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/session/login", json={"identity_token": _identity_token(key)}
        )

    assert response.status_code == 503


async def test_secure_can_be_turned_off_only_deliberately(monkeypatch, key):
    """The dev-only escape hatch, pinned so it stays opt-*out*."""
    monkeypatch.setenv(session_router.ENV_COOKIE_SECURE, "false")
    gateway = _FakeGateway({"exists": True, "active": True, "role": "internal"})
    transport = ASGITransport(app=_app(gateway))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/session/login", json={"identity_token": _identity_token(key)}
        )

    assert "secure" not in response.headers["set-cookie"].lower()
