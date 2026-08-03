"""The session BFF — this service's own half of sign-in (task E8).

Sign-in happens at the gateway. What arrives here is the *result*: a
short-lived RS256 identity token, audience ``cloud-agent``, signed by a key
this service only ever holds the public half of. Three routes trade it for a
cookie and answer questions about that cookie:

- ``POST /v1/session/login`` — verify the identity token, ask the gateway
  whether this account may sign in, and set the cookie.
- ``POST /v1/session/logout`` — clear it.
- ``GET  /v1/session/me`` — who the cookie says we are, or 401.

**Why the gateway is asked at login.** Verifying the identity token proves who
someone is, not that they may use anything: the token is minted at
authentication time and says nothing about a suspension that happened since, or
about a role that changed. The cookie it buys lasts seven days. So login is the
one moment where a status call is both cheap (once per sign-in, not once per
request) and load-bearing (a suspended account must not walk away with a
week-long credential), and the role written into the session comes from the
gateway's answer rather than from a claim in the token — the role is what gates
the internal-only dogfood surface, so it should come from the service that owns
it.

It **fails closed**, like every other gateway call in this service: a gateway
that cannot be reached mints no session. That is a worse day for someone trying
to sign in and a much better one for everybody else, and it is the whole point
of AGENTS.md rule 4 — an unreachable authority means no permission, never
assumed permission.

**A 401 from ``/me`` is not an error.** It is the answer "not signed in", which
is what the browser asks on every load. ``ProtectedRoute`` loops if that ever
arrives as a 500, so the failure taxonomy here is deliberately narrow: 401 for
"no or bad session", 503 only for "this deployment cannot verify sessions at
all".
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from cloud_agent import identity
from cloud_agent.api.deps import get_current_user, get_gateway_client
from cloud_agent.gateway import GatewayError
from cloud_agent.logging_utils import get_logger
from cloud_agent.schemas import SessionLoginRequest, SessionUserResponse

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/session")

#: Dev-only escape hatch. ``Secure`` is the default because a session cookie
#: sent over plaintext is a session cookie anyone on the path can take; a
#: developer running the whole stack on ``http://localhost`` is the one case
#: where that is not true, and they have to say so out loud.
ENV_COOKIE_SECURE = "AGENT_SESSION_COOKIE_SECURE"

#: The statuses the gateway may report that still mean "may sign in". Anything
#: else — suspended, banned, deleted, unknown — is refused, because an
#: allowlist is the only version of this check that fails closed when the
#: gateway grows a new status nobody here has heard of.
_ACTIVE_STATUSES = frozenset({"active", "ok", "enabled"})


def _cookie_is_secure() -> bool:
    """Whether the session cookie carries ``Secure``."""
    raw = (os.environ.get(ENV_COOKIE_SECURE) or "").strip().lower()
    return raw not in ("0", "false", "no")


def _may_sign_in(status: dict[str, Any]) -> bool:
    """Read the gateway's status record as a yes or a no.

    Written against the shape the client's own tests pin —
    ``{"user_id", "exists", "active", "role"}`` — and tolerant of a record that
    says the same thing with a ``status`` string instead. Every branch that
    cannot find an affirmative answer returns ``False``.
    """
    if status.get("exists") is False:
        return False
    active = status.get("active")
    if isinstance(active, bool):
        return active
    named = status.get("status")
    if isinstance(named, str):
        return named.lower() in _ACTIVE_STATUSES
    # No recognizable statement either way. Refuse: a record this service
    # cannot read is not a record that granted anything.
    return False


@router.post("/login", response_model=SessionUserResponse)
async def session_login(
    body: SessionLoginRequest,
    response: Response,
    gateway=Depends(get_gateway_client),
) -> SessionUserResponse:
    """Trade a gateway identity token for this service's session cookie.

    Args:
        body: Carries the identity token the callback page obtained.
        response: The response the cookie is set on.
        gateway: The gateway's internal contracts.

    Returns:
        The signed-in user, in the shape ``session.ts`` expects.

    Raises:
        HTTPException: 401 if the identity token does not verify, 403 if the
            gateway says this account may not sign in, 503 if this deployment
            cannot verify identity or could not reach the gateway.
    """
    try:
        claims = identity.verify_identity_token(body.identity_token)
    except identity.IdentityNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except identity.IdentityError:
        # Deliberately not echoing the reason. "Signed by an unknown key" and
        # "expired" are the same answer to a browser, and the difference is
        # useful mostly to somebody probing.
        raise HTTPException(status_code=401, detail="Invalid identity token.") from None

    user_id = str(claims.get("sub") or "")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid identity token.")

    try:
        status = await gateway.user_status(user_id)
    except GatewayError as exc:
        logger.warning(
            "agent_session_status_unavailable",
            exc_info=True,
            extra={"event": "agent_session_status_unavailable", "user_id": user_id},
        )
        raise HTTPException(
            status_code=503,
            detail="Cannot confirm this account with the gateway; try again shortly.",
        ) from exc

    if not _may_sign_in(status):
        raise HTTPException(status_code=403, detail="This account cannot sign in.")

    # The gateway's role, not the token's. The identity token proves who; the
    # status record is what this deployment's role gates should believe.
    role = str(status.get("role") or "free")
    email = status.get("email") or claims.get("email")
    email = str(email) if email else None

    try:
        session = identity.mint_session(user_id=user_id, email=email, role=role)
    except identity.IdentityNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None

    response.set_cookie(
        identity.SESSION_COOKIE,
        session,
        max_age=identity.SESSION_TTL_S,
        httponly=True,
        # Lax, not Strict: the return leg of sign-in is a top-level navigation
        # back from the gateway, and Strict would drop the cookie on exactly
        # that request. Lax still refuses to send it on a cross-site POST,
        # which is the request a CSRF would have to be.
        samesite="lax",
        secure=_cookie_is_secure(),
        path="/",
    )
    return SessionUserResponse(
        id=user_id,
        email=email,
        role=role,
        user_name=(str(status["user_name"]) if status.get("user_name") else None),
    )


@router.post("/logout")
async def session_logout(response: Response) -> dict[str, bool]:
    """Clear the session cookie.

    Unauthenticated on purpose: signing out must work when the session is
    already expired or malformed, which is exactly when a browser most wants
    it. There is nothing to protect — the only effect is deleting a cookie the
    caller already holds.

    The attributes must match the ones the cookie was set with, or the browser
    keeps the original and the user stays signed in.
    """
    response.delete_cookie(
        identity.SESSION_COOKIE,
        path="/",
        httponly=True,
        samesite="lax",
        secure=_cookie_is_secure(),
    )
    return {"ok": True}


@router.get("/me", response_model=SessionUserResponse)
async def session_me(request: Request) -> SessionUserResponse:
    """Return the current user, or 401 when there is no valid session.

    No gateway call. The account was checked at login and the session's own
    expiry bounds how stale that answer can get; putting a gateway round trip
    on the call every page load makes the whole app unavailable whenever the
    gateway is, for a question already answered.
    """
    user = await get_current_user(
        request=request, authorization=request.headers.get("authorization")
    )
    return SessionUserResponse(
        id=user["user_id"],
        email=user.get("email"),
        role=user["role"],
        user_name=user.get("user_name"),
    )
