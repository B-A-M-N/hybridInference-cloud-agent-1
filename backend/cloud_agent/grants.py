"""Asking the gateway for the capability a sandbox runs with.

This is where the per-attempt model credential comes from now. Before the
split the control plane minted it itself, signed with a key it shared with the
gateway. It no longer holds that key, and should not: minting a credential
that spends someone's inference quota is the gateway's decision, and asking
for one is this service's.

Three calls, and the asymmetry between them is deliberate:

- **mint** is idempotent on ``(job, attempt)``. A retry after a timeout gets
  the same grant back rather than a second capability nothing is tracking.
- **renew** is what keeps a long job alive. The grant's TTL is short, so a
  control plane that stops renewing — crashed, partitioned, or having decided
  the attempt is superseded — lets the capability die on its own.
- **revoke** is an *acceleration* of that, not the mechanism. It is allowed to
  fail: the TTL is the backstop, which is the only reason revoke needs no
  durable outbox behind it.

**The fence lives here, not at the gateway.** After the split the gateway holds
no attempt or lease state at all, so nothing there can notice that an attempt
was superseded. This service notices, and stops renewing. Anything that makes
renewal automatic — a background loop that does not consult the store — quietly
removes the fence.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from cloud_agent.gateway import ENV_BASE_URL, ENV_DISPATCH_TOKEN, GatewayError
from cloud_agent.logging_utils import get_logger

logger = get_logger(__name__)

_TIMEOUT_S = 10.0
_CONNECT_TIMEOUT_S = 5.0


class GrantClient:
    """Mints, renews and revokes inference grants at the gateway."""

    def __init__(
        self,
        base_url: str | None = None,
        dispatch_token: str | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Build a client.

        Args:
            base_url: Gateway origin; defaults to ``GATEWAY_BASE_URL``.
            dispatch_token: Service credential; defaults to
                ``GATEWAY_GRANT_DISPATCH_TOKEN``.
            client: An existing httpx client, for tests and pooling.
        """
        self._base_url = (base_url or os.environ.get(ENV_BASE_URL) or "").rstrip("/")
        self._token = dispatch_token or os.environ.get(ENV_DISPATCH_TOKEN) or ""
        self._client = client

    @property
    def configured(self) -> bool:
        """Whether this deployment can ask for grants at all."""
        return bool(self._base_url and self._token)

    async def _post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Perform one authorized POST, or raise.

        Raises:
            GatewayError: If unconfigured, unreachable, refused, or unparsable.
        """
        if not self.configured:
            raise GatewayError(f"{ENV_BASE_URL} and {ENV_DISPATCH_TOKEN} must both be set")
        headers = {"Authorization": f"Bearer {self._token}"}
        timeout = httpx.Timeout(_TIMEOUT_S, connect=_CONNECT_TIMEOUT_S)
        url = f"{self._base_url}{path}"
        try:
            if self._client is not None:
                response = await self._client.post(url, json=body, headers=headers, timeout=timeout)
            else:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.post(url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise GatewayError(f"gateway unreachable: {exc}") from exc

        if response.status_code != 200:
            # The gateway's refusal reason is worth surfacing: "that user
            # cannot be granted capability" and "unknown MCP server" are
            # different problems for whoever created the job.
            detail = ""
            try:
                payload = response.json()
                if isinstance(payload, dict):
                    detail = str(payload.get("error", {}).get("message", ""))
            except ValueError:
                detail = ""
            raise GatewayError(
                f"gateway answered {response.status_code} for {path}"
                + (f": {detail}" if detail else "")
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise GatewayError(f"gateway returned unparsable JSON for {path}") from exc
        if not isinstance(payload, dict):
            raise GatewayError(f"gateway returned {type(payload).__name__} for {path}")
        return payload

    async def mint(
        self,
        *,
        user_id: str,
        job_id: str,
        attempt_id: str,
        models: list[str] | None = None,
        mcp_servers: list[str] | None = None,
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Ask for the capability one attempt runs with.

        The returned scope is the **effective** one, which may be narrower than
        what was asked: the gateway clamps models to what the user's role may
        reach. Use what comes back, not what was sent — a job built around a
        model the grant does not carry fails at its first call.

        An unknown MCP server is refused outright rather than dropped, so a
        failure here means the job asked for a tool surface this deployment
        does not offer, and it should fail at creation rather than mid-run.

        Args:
            user_id: The gateway's user identifier.
            job_id: This service's job id, opaque to the gateway.
            attempt_id: This service's attempt id.
            models: Requested models; ``None`` means everything the role allows.
            mcp_servers: Requested MCP servers; ``None`` means the deployment's
                defaults.
            ttl_seconds: Requested lifetime, clamped by the gateway.

        Returns:
            The grant, including its bearer ``token``.

        Raises:
            GatewayError: If the grant cannot be minted.
        """
        body: dict[str, Any] = {
            "user_id": user_id,
            "external_job_id": job_id,
            "external_attempt_id": str(attempt_id),
        }
        if models is not None:
            body["allowed_models"] = models
        if mcp_servers is not None:
            body["allowed_mcp"] = mcp_servers
        if ttl_seconds is not None:
            body["ttl_seconds"] = ttl_seconds
        return await self._post("/internal/agent-grants", body)

    async def renew(self, grant_id: str) -> dict[str, Any]:
        """Extend a live grant by another bounded step.

        Call this only while the attempt is still current. Renewing an attempt
        the store has superseded is what would keep a zombie sandbox spending,
        and the gateway cannot catch it: it has no attempt state to check.

        Args:
            grant_id: The grant to extend.

        Returns:
            The renewed grant.

        Raises:
            GatewayError: If the grant is unknown, revoked, expired, or its
                owner is no longer active.
        """
        return await self._post(f"/internal/agent-grants/{grant_id}/renew")

    async def revoke(self, grant_id: str) -> bool:
        """Withdraw a grant now rather than waiting for its TTL.

        Returns ``False`` instead of raising when the call fails. Revocation is
        an acceleration of something the TTL does anyway, so a failure here
        must not take down the caller — which is usually a cleanup path that
        has other work to finish.

        Args:
            grant_id: The grant to revoke.

        Returns:
            Whether the gateway confirmed the revocation.
        """
        try:
            await self._post(f"/internal/agent-grants/{grant_id}/revoke")
        except GatewayError:
            logger.warning(
                "agent_grant_revoke_failed",
                exc_info=True,
                extra={"event": "agent_grant_revoke_failed", "grant_id": grant_id},
            )
            return False
        return True
