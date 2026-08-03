"""Dependencies the routers ask for, and the ones that do not exist yet.

The router moved from the gateway unchanged, so it still asks for the
dependencies it asked for there. Two kinds arrive at this module:

- **Wiring this service owns.** The job store, the GitHub App credential
  minter, the GitLab OAuth client and the gateway client are all objects this
  repository builds; they are read off ``app.state``, exactly as the source read
  them off its own services container. ``None`` is a working answer for the
  optional ones — a deployment without a configured database or App is a
  supported shape, and the routes already handle it. The four attribute names
  (``store``, ``gateway``, ``app_credentials``, ``gitlab_oauth``) are the
  contract with ``app.py``'s lifespan; renaming one there without renaming it
  here degrades silently to "not configured" rather than failing.

- **Identity, admin auth and the billing ledger, which stayed on the gateway.**
  Those are placeholders that raise. They keep the *names and signatures* the
  router calls so that the moved file needs no edits when they are filled in,
  and they raise rather than return a permissive stand-in because a migration
  stub that quietly authenticates everyone is the one failure mode nobody
  notices. Each says which task owns it:

  - **E7** (identity adapter) owns ``get_current_user``, ``verify_api_key``,
    ``verify_admin_access``, ``get_operational_store``, ``has_role``,
    ``looks_like_agent_token``, ``log_admin_action`` and ``get_client_ip``.
  - **E9** (grant client) owns ``get_log_store``: job spend was read from the
    gateway's ``api_logs`` ledger, and after the split the budget is the
    grant's.

Until those land the HTTP surface imports, mounts and documents itself, and
every authenticated route fails loudly at the first request.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import Depends, Header, Request

if TYPE_CHECKING:
    from cloud_agent.gateway import GatewayClient
    from cloud_agent.storage.store import AgentJobStore

# Raised by every placeholder below, so a half-wired deployment fails at the
# call rather than at some later assertion about what the caller was allowed to
# do. The message names the task, because "NotImplementedError" in a traceback
# is not an instruction.
_PENDING = "{name}() is not wired yet — {task} fills it in ({what})."


def _pending(name: str, task: str, what: str) -> NotImplementedError:
    """Build the refusal a not-yet-migrated dependency raises."""
    return NotImplementedError(_PENDING.format(name=name, task=task, what=what))


# ── Wiring this service owns ───────────────────────────────────────────


def get_agent_job_store(request: Request) -> AgentJobStore | None:
    """Dependency to obtain the agent job store (if configured)."""
    return getattr(request.app.state, "store", None)


def get_gateway_client(request: Request) -> GatewayClient:
    """Dependency to obtain the client for the gateway's internal contracts.

    Not optional: the catalog, the user's status and the MCP registry are all
    answered over HTTP now, so a control plane with no gateway client cannot
    answer for a job at all. An unconfigured client still constructs, and
    refuses at call time naming the variables it needs — which is where the
    operator can act on it, unlike a dependency that returned ``None``.
    """
    client = getattr(request.app.state, "gateway", None)
    if client is None:
        from cloud_agent.gateway import GatewayClient as _GatewayClient

        client = _GatewayClient()
        request.app.state.gateway = client
    return client


def get_agent_app_credentials(request: Request) -> Any | None:
    """Dependency to obtain the GitHub App credential minter (if configured)."""
    return getattr(request.app.state, "app_credentials", None)


def get_agent_gitlab_oauth(request: Request) -> Any | None:
    """Dependency to obtain the configured GitLab.com OAuth client."""
    return getattr(request.app.state, "gitlab_oauth", None)


# ── Billing ledger — E9 ────────────────────────────────────────────────


def get_log_store(request: Request) -> Any | None:
    """Dependency to obtain the log store (if configured)."""
    raise _pending(
        "get_log_store",
        "E9",
        "job spend was summed from the gateway's api_logs; the grant carries the budget now",
    )


# ── Identity and admin auth — E7 ───────────────────────────────────────


def get_operational_store(request: Request) -> Any | None:
    """Dependency to obtain the operational store (if configured)."""
    raise _pending(
        "get_operational_store",
        "E7",
        "the gateway owns users and api_keys; this service verifies an identity JWT instead",
    )


async def get_current_user(
    authorization: str | None = Header(None),
    op_store=Depends(get_operational_store),
) -> dict[str, Any]:
    """Verify a browser access token and return the current user context."""
    raise _pending(
        "get_current_user",
        "E7",
        "verify the identity JWT against GATEWAY_JWKS_URL and upsert the local user row",
    )


async def verify_api_key(
    request: Request,
    authorization: str | None = Header(None),
    x_api_key: str | None = Header(None, alias="X-API-Key"),
    x_on_behalf_of: str | None = Header(None, alias="X-On-Behalf-Of"),
    op_store=Depends(get_operational_store),
    log_store=Depends(get_log_store),
    agent_job_store=Depends(get_agent_job_store),
) -> dict[str, Any]:
    """Verify an API key and return the user context it authenticates."""
    raise _pending(
        "verify_api_key",
        "E7",
        "the api_keys table stayed on the gateway; this surface takes identity JWTs",
    )


async def verify_admin_access(
    request: Request,
    authorization: str | None = Header(None),
    op_store=Depends(get_operational_store),
) -> str:
    """Authenticate an admin caller, returning an identifier for the audit log."""
    raise _pending(
        "verify_admin_access",
        "E7",
        "admin role comes from the identity JWT, not from the gateway's users table",
    )


def has_role(user_role: str, required: str) -> bool:
    """Whether ``user_role`` meets or exceeds the required role level."""
    raise _pending(
        "has_role",
        "E7",
        "the role hierarchy arrives with the identity claims",
    )


def looks_like_agent_token(api_key: str | None) -> bool:
    """Whether a credential should be resolved as a worker token."""
    raise _pending(
        "looks_like_agent_token",
        "E7",
        "it separates credential namespaces in the auth dispatch E7 replaces",
    )


async def log_admin_action(
    db_logger: Any,
    admin_ip: str,
    action: str,
    target_user_id: str | None = None,
    details: dict[str, Any] | None = None,
    success: bool = True,
) -> None:
    """Append one admin action to the audit trail."""
    raise _pending(
        "log_admin_action",
        "E7",
        "the admin audit trail is a gateway table; this service needs its own",
    )


def get_client_ip(request: Request) -> str:
    """Return the best-effort originating client IP for a request."""
    raise _pending(
        "get_client_ip",
        "E7",
        "the proxy-header resolution rules did not migrate with the router",
    )
