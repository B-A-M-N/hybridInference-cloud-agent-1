"""Verifying the gateway's identity tokens, and minting our own session.

Two credentials, deliberately different lifetimes and audiences.

The **identity token** is the gateway's: RS256, short-lived, `aud=cloud-agent`,
verified against its JWKS. It says who a person is and nothing about what they
may do here. This service never holds the key that signs it — only the public
half, fetched from a URL — which is the property that lets the two services
share a login without sharing a secret.

The **session cookie** is ours: HttpOnly, signed with our own secret, and the
only credential the browser keeps. The identity token is exchanged for one and
then discarded, so a token intercepted afterwards is already spent.

**Keys are cached by ``kid`` and re-fetched on a miss.** That is what makes
rotation work without coordination: the gateway starts signing with a new key,
the first token carrying an unknown ``kid`` triggers one fetch, and tokens
already in flight keep verifying against the retiring key the gateway is still
publishing.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx
import jwt
from jwt import PyJWKClient

from cloud_agent.logging_utils import get_logger

logger = get_logger(__name__)

ENV_JWKS_URL = "GATEWAY_JWKS_URL"
ENV_ISSUER = "GATEWAY_ISSUER"
ENV_SESSION_SECRET = "AGENT_SESSION_SECRET"

#: Must match what the gateway mints. A token for a different audience is a
#: token for a different service, and accepting it would let one consumer's
#: credential act as another's.
AUDIENCE = "cloud-agent"

#: Our own session. Long enough to be useful, short enough that a stolen cookie
#: expires without anyone having to notice.
SESSION_TTL_S = 7 * 24 * 60 * 60
SESSION_COOKIE = "cloud_agent_session"


class IdentityError(Exception):
    """An identity token or session could not be verified."""


class IdentityNotConfigured(IdentityError):
    """This deployment cannot verify identity at all."""


_jwk_client: PyJWKClient | None = None
_jwk_client_url: str = ""


def _jwks_client() -> PyJWKClient:
    """Return the JWKS client, built once per URL.

    Raises:
        IdentityNotConfigured: If no JWKS URL is set.
    """
    global _jwk_client, _jwk_client_url

    url = (os.environ.get(ENV_JWKS_URL) or "").strip()
    if not url:
        raise IdentityNotConfigured(f"{ENV_JWKS_URL} must be set to verify identity tokens")
    if _jwk_client is None or _jwk_client_url != url:
        # PyJWKClient caches keys and re-fetches on an unknown kid, which is
        # exactly the rotation behaviour wanted here.
        _jwk_client = PyJWKClient(url, cache_keys=True)
        _jwk_client_url = url
    return _jwk_client


def reset_jwks_cache() -> None:
    """Drop the cached JWKS client. For tests that change the URL."""
    global _jwk_client, _jwk_client_url
    _jwk_client = None
    _jwk_client_url = ""


def verify_identity_token(token: str) -> dict[str, Any]:
    """Verify a gateway identity token and return its claims.

    Args:
        token: The RS256 JWT the gateway issued.

    Returns:
        The verified claims.

    Raises:
        IdentityNotConfigured: If this deployment has no JWKS URL.
        IdentityError: If the token is missing, malformed, expired, signed by
            an unknown key, or issued for another audience.
    """
    if not token:
        raise IdentityError("no identity token presented")
    try:
        signing_key = _jwks_client().get_signing_key_from_jwt(token)
    except IdentityNotConfigured:
        raise
    except (jwt.PyJWTError, httpx.HTTPError, Exception) as exc:
        # An unfetchable or unknown key is a refusal, never a pass. Failing
        # open here would accept any token during a gateway outage.
        raise IdentityError(f"cannot resolve the signing key: {exc}") from exc

    issuer = (os.environ.get(ENV_ISSUER) or "").strip()
    try:
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=AUDIENCE,
            # Issuer is checked when configured. Left optional because a
            # deployment may front the gateway at a different name than it
            # mints; pinning it wrongly locks everyone out.
            issuer=issuer or None,
            options={"require": ["exp", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise IdentityError(f"invalid identity token: {exc}") from exc


#: RFC 7518 §3.2 for HS256. PyJWT only *warns* below this, and a warning in a
#: deploy log is not a control — a short secret is brute-forceable, and every
#: session it signs is forgeable.
MIN_SESSION_SECRET_BYTES = 32


def _session_secret() -> str:
    """Return the session signing secret.

    Raises:
        IdentityNotConfigured: If unset or too short. Refusing beats
            defaulting — a default would make every deployment able to forge
            every other's sessions — and refusing beats warning, which is what
            the JWT library does on its own.
    """
    secret = (os.environ.get(ENV_SESSION_SECRET) or "").strip()
    if not secret:
        raise IdentityNotConfigured(f"{ENV_SESSION_SECRET} must be set to issue sessions")
    if len(secret.encode()) < MIN_SESSION_SECRET_BYTES:
        raise IdentityNotConfigured(
            f"{ENV_SESSION_SECRET} must be at least {MIN_SESSION_SECRET_BYTES} bytes; "
            "generate one with `openssl rand -base64 32`"
        )
    return secret


def mint_session(
    *, user_id: str, email: str | None, role: str, user_name: str | None = None
) -> str:
    """Mint the session token the browser holds.

    HS256 rather than RS256: this token is issued and verified by the same
    service, so there is nothing to publish and a symmetric key is the simpler
    correct choice.

    **Everything the session routes answer with is minted in here.** ``/me`` has
    nothing but these claims to read, so anything login reports that this token
    does not carry is something the two routes will disagree about for the whole
    life of the cookie.

    Args:
        user_id: The gateway's user identifier, carried as ``sub``.
        email: The user's email, if known.
        role: The user's role at the time of sign-in.
        user_name: The user's display name, if the gateway reported one.
            Omitted from the claims when absent — which is every deployment
            today, since neither the identity token nor the user-status record
            carries a name.

    Returns:
        A signed session token.

    Raises:
        IdentityNotConfigured: If no session secret is set.
    """
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": user_id,
        "role": role,
        "iat": now,
        "exp": now + SESSION_TTL_S,
    }
    if email:
        claims["email"] = email
    if user_name:
        claims["user_name"] = user_name
    return jwt.encode(claims, _session_secret(), algorithm="HS256")


def verify_session(token: str) -> dict[str, Any]:
    """Verify a session token and return its claims.

    Args:
        token: The cookie's value.

    Returns:
        The verified claims.

    Raises:
        IdentityNotConfigured: If no session secret is set.
        IdentityError: If the session is missing, malformed, or expired.
    """
    if not token:
        raise IdentityError("no session presented")
    try:
        return jwt.decode(
            token,
            _session_secret(),
            algorithms=["HS256"],
            options={"require": ["exp", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise IdentityError(f"invalid session: {exc}") from exc
