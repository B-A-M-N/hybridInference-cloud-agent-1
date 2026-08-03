"""Stand-ins for the auth dependencies E7 has not migrated yet.

The router moved from the gateway unmodified, so it still calls
``get_current_user``, ``verify_api_key``, ``verify_admin_access``,
``has_role``, ``looks_like_agent_token``, ``get_client_ip`` and
``log_admin_action``. In this repository all seven are placeholders in
``cloud_agent.api.deps`` that raise ``NotImplementedError`` — deliberately, so
a half-wired deployment fails loudly instead of authenticating everyone.

E10 ports the API-surface tests, which exercise the router *through* those
dependencies. The test app therefore supplies them. What follows is not a
"fake auth for tests" in the usual sense: it is the reduced behaviour of the
gateway dependencies the router still expects, so the moved test files can run
against the moved router with their assertions untouched.

**Read this before trusting a green run.** Three different things are pinned by
the tests that use this module, and only two of them are coverage of code in
this repository:

1. **The router's own credential dispatch.** ``authenticate_agent_owner``,
   ``_looks_like_browser_jwt``, ``require_agent_owner`` and
   ``verify_dispatcher_access`` all live in ``cloud_agent/api/routes.py``.
   Every assertion about *which* authenticator a credential reaches, and about
   the claim gate's dedicated-token fast path, is real coverage of this
   service.
2. **Wire-level authorization outcomes.** 401/403/503 status codes on the
   routes are produced by the router and its dependency graph, so they are
   real too.
3. **The contract E7 must satisfy.** A handful of assertions key on the
   *message* the gateway's authenticator produced ("Token has expired…"), or
   on a side effect that belongs to a table which stayed on the gateway
   (``update_key_last_used``). Those are reproduced faithfully here, which
   means the assertion now describes what E7 has to implement rather than
   testing something that already exists. They are listed in
   ``MIGRATION.md``'s E10 record so the distinction does not get lost.

Nothing here reads a gateway secret. The role hierarchy and the token-prefix
test are copied verbatim from the source because they are constants, not
credentials; the API-key hash is a plain SHA-256 because the fake operational
stores in the tests ignore its value, and deriving the gateway's real hash
would require ``API_KEY_SECRET`` — which AGENTS.md rule 4 forbids this
repository from holding.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import TYPE_CHECKING, Any

from fastapi import Header, HTTPException, Request

if TYPE_CHECKING:
    import pytest

# Verbatim from the source's serving/config/settings.py. Fail-closed on an
# unknown *required* role; an unknown *user* role ranks as free.
ROLE_RANK: dict[str, int] = {"free": 0, "pro": 1, "internal": 2, "admin": 3}

#: The namespace worker tokens are minted into (``cloud_agent.tokens``).
AGENT_TOKEN_PREFIX = "ajt."


def has_role(user_role: str, required: str) -> bool:
    """Whether ``user_role`` meets or exceeds ``required``."""
    if required not in ROLE_RANK:
        return False
    return ROLE_RANK.get(user_role, 0) >= ROLE_RANK[required]


def looks_like_agent_token(api_key: str | None) -> bool:
    """Whether a credential should be resolved as a worker token."""
    return bool(api_key) and api_key.startswith(AGENT_TOKEN_PREFIX)


def hash_api_key(api_key: str) -> str:
    """Hash a presented API key for the operational store lookup."""
    return hashlib.sha256(api_key.encode()).hexdigest()


async def get_current_user(
    authorization: str | None = None,
    op_store: Any = None,
) -> dict[str, Any]:
    """Resolve a browser session token into a user context.

    The gateway verified its own access JWT here. This service's browser
    credential is the session token ``cloud_agent.identity`` mints, so that is
    what this verifies; the surrounding failure taxonomy — and its messages —
    are the source's, because the ported tests assert on them.
    """
    import jwt

    from cloud_agent.identity import verify_session

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Missing authentication token. Use 'Authorization: Bearer {token}' header.",
        )

    token = authorization[7:]
    try:
        payload = verify_session(token)
    except Exception as exc:
        # An expired session must stay an expired session: the whole point of
        # the dispatch under test is that it is not retried as an API key.
        cause = exc.__cause__ if exc.__cause__ is not None else exc
        if isinstance(cause, jwt.ExpiredSignatureError):
            raise HTTPException(
                status_code=401,
                detail="Token has expired. Please refresh your token or login again.",
            ) from None
        raise HTTPException(status_code=401, detail="Invalid authentication token.") from None

    user_id = payload.get("sub")
    email = payload.get("email")
    if not user_id or not email:
        raise HTTPException(status_code=401, detail="Invalid token payload.")

    if not op_store:
        raise HTTPException(status_code=503, detail="Authentication database unavailable.")

    user_row = await op_store.get_user_by_id(user_id)
    if not user_row:
        raise HTTPException(status_code=401, detail="User not found.")
    if user_row["status"] != "active":
        raise HTTPException(
            status_code=403,
            detail=f"Account is {user_row['status']}. Please contact support.",
        )

    user_role = user_row["role"] or "free"
    return {
        "user_id": user_id,
        "email": user_row["email"],
        "role": user_role,
        "is_admin": user_role == "admin",
        "email_verified": user_row["email_verified"],
        "status": user_row["status"],
    }


async def verify_api_key(
    request: Any = None,
    authorization: str | None = None,
    x_api_key: str | None = None,
    op_store: Any = None,
    log_store: Any = None,
    agent_job_store: Any = None,
) -> dict[str, Any]:
    """Resolve a programmatic API key into a user context.

    Two behaviours the ported tests depend on:

    - **A worker token is never an owner credential.** On the gateway this was
      an allowlist of inference paths, because that dependency also guarded
      ``/v1/chat/completions``. This service serves no inference route at all,
      so the rule collapses to a flat refusal — the same 403 and the same
      ``insufficient_scope`` type the sandbox would have got there.
    - **The presented key's ``last_used`` is touched.** That column lives in
      the gateway's ``api_keys`` table; reproduced here because the assertion
      moved with the test.
    """
    presented = None
    if authorization and authorization.startswith("Bearer "):
        presented = authorization[7:]
    elif x_api_key:
        presented = x_api_key

    if looks_like_agent_token(presented):
        raise HTTPException(
            status_code=403,
            detail={
                "error": {
                    "type": "insufficient_scope",
                    "message": "This credential may only be used for model inference.",
                }
            },
        )

    if not presented:
        raise HTTPException(
            status_code=401,
            detail="Missing API key. Use 'Authorization: Bearer hyi-xxx' or 'X-API-Key: hyi-xxx'",
        )

    if not op_store:
        raise HTTPException(status_code=503, detail="Authentication database unavailable.")

    caller = await op_store.get_auth_context_by_key_hash(hash_api_key(presented))
    if not caller:
        raise HTTPException(status_code=401, detail="Invalid or expired API key")

    await op_store.update_key_last_used(caller["id"])

    user_role = caller.get("role") or "free"
    return {
        "user_id": caller["user_id"],
        "user_name": caller.get("user_name"),
        "role": user_role,
        "authenticated": True,
        "is_admin": user_role == "admin",
    }


def _check_admin_token(authorization: str | None) -> str:
    """Authenticate an admin caller by the deployment's ``ADMIN_TOKEN``.

    The gateway also accepted an admin user's JWT. No ported test exercises
    that half, and inventing one here would be inventing E7's design, so this
    keeps only the machine-to-machine door the tests actually knock on.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Missing authentication. Provide a JWT or admin token via Authorization header.",
        )
    token = authorization[7:]
    admin_token = (os.environ.get("ADMIN_TOKEN") or "").strip()
    if not admin_token or not hmac.compare_digest(token, admin_token):
        raise HTTPException(status_code=401, detail="Invalid authentication token.")
    return "admin-token"


async def verify_admin_access(
    request: Request,
    authorization: str | None = Header(None),
) -> str:
    """FastAPI-facing admin gate, for ``app.dependency_overrides``."""
    return _check_admin_token(authorization)


async def verify_admin_access_direct(
    request: Any = None,
    authorization: str | None = None,
    op_store: Any = None,
) -> str:
    """Directly-callable admin gate.

    ``verify_dispatcher_access`` calls ``verify_admin_access`` as a plain
    function rather than through injection, so its fallback path needs a
    callable with the source's keyword signature.
    """
    return _check_admin_token(authorization)


def get_client_ip(request: Any) -> str:
    """Best-effort client IP, for the admin audit trail."""
    client = getattr(request, "client", None)
    return getattr(client, "host", None) or "unknown"


async def log_admin_action(
    db_logger: Any,
    admin_ip: str,
    action: str,
    target_user_id: str | None = None,
    details: dict[str, Any] | None = None,
    success: bool = True,
) -> None:
    """Append one admin action to the audit trail — a no-op until E7 has one."""
    return None


def install(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the router modules at the stand-ins.

    ``app.dependency_overrides`` cannot reach these: the router binds them at
    import time and calls them as plain functions (``authenticate_agent_owner``
    calls ``get_current_user``/``verify_api_key`` itself, ``admin_hosts`` calls
    ``log_admin_action``). Only the ones the router declares with ``Depends``
    are overridable, and those are handled per test app.
    """
    from cloud_agent.api import admin_hosts, deps, routes

    monkeypatch.setattr(routes, "get_current_user", get_current_user)
    monkeypatch.setattr(routes, "verify_api_key", verify_api_key)
    monkeypatch.setattr(routes, "verify_admin_access", verify_admin_access_direct)
    monkeypatch.setattr(routes, "looks_like_agent_token", looks_like_agent_token)
    # require_agent_owner imports has_role from the deps module at call time.
    monkeypatch.setattr(deps, "has_role", has_role)
    monkeypatch.setattr(admin_hosts, "get_client_ip", get_client_ip)
    monkeypatch.setattr(admin_hosts, "log_admin_action", log_admin_action)
