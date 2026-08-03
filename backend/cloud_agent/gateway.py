"""The HTTP client for everything this service asks the gateway.

Before the split these were imports. The rule now is that the gateway is
reachable over HTTP only, so every question that used to be answered by
reading its registries or its users table comes through here.

**Every failure is closed.** A gateway that is down, slow, or answering
strangely must never widen what this service permits: an unreachable catalog
resolves to *no* models, not every model, and an unreadable user status
resolves to "cannot use the service", not "fine". The alternative fails open
at exactly the moment nobody is watching.

Authorization is the shared dispatch token. It is a service credential, so it
travels only on this path and never toward a sandbox or a browser.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from cloud_agent.logging_utils import get_logger

logger = get_logger(__name__)

ENV_BASE_URL = "GATEWAY_BASE_URL"
ENV_DISPATCH_TOKEN = "GATEWAY_GRANT_DISPATCH_TOKEN"

#: Generous enough for a busy gateway, bounded because a control plane blocked
#: on a lookup is a control plane that has stopped scheduling.
_TIMEOUT_S = 10.0
_CONNECT_TIMEOUT_S = 5.0


class GatewayError(Exception):
    """The gateway could not answer, or answered unusably."""


class GatewayClient:
    """Calls the gateway's internal contracts on behalf of this service."""

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
            client: An existing httpx client, for tests and for sharing a
                connection pool.
        """
        self._base_url = (base_url or os.environ.get(ENV_BASE_URL) or "").rstrip("/")
        self._token = dispatch_token or os.environ.get(ENV_DISPATCH_TOKEN) or ""
        self._client = client

    @property
    def configured(self) -> bool:
        """Whether this deployment knows how to reach a gateway at all."""
        return bool(self._base_url and self._token)

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Perform one authorized GET, or raise.

        Raises:
            GatewayError: If unconfigured, unreachable, refused, or unparsable.
        """
        if not self.configured:
            raise GatewayError(f"{ENV_BASE_URL} and {ENV_DISPATCH_TOKEN} must both be set")
        headers = {"Authorization": f"Bearer {self._token}"}
        timeout = httpx.Timeout(_TIMEOUT_S, connect=_CONNECT_TIMEOUT_S)
        try:
            if self._client is not None:
                response = await self._client.get(
                    f"{self._base_url}{path}", params=params, headers=headers, timeout=timeout
                )
            else:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.get(
                        f"{self._base_url}{path}", params=params, headers=headers
                    )
        except httpx.HTTPError as exc:
            raise GatewayError(f"gateway unreachable: {exc}") from exc

        if response.status_code != 200:
            raise GatewayError(f"gateway answered {response.status_code} for {path}")
        try:
            body = response.json()
        except ValueError as exc:
            raise GatewayError(f"gateway returned unparsable JSON for {path}") from exc
        if not isinstance(body, dict):
            raise GatewayError(f"gateway returned {type(body).__name__} for {path}")
        return body

    async def model_catalog(self, user_id: str) -> list[str]:
        """Return the chat models this user's agent jobs may call.

        Deliberately not ``GET /v1/models``: that route cannot authenticate a
        service credential and would answer with the *anonymous* catalog,
        silently narrowing every non-free user to what a stranger sees.

        Args:
            user_id: The gateway's user identifier.

        Returns:
            Canonical model ids, in the gateway's registry order.

        Raises:
            GatewayError: If the catalog cannot be read. Callers that must not
                fail hard should use :func:`agent_visible_models`, which
                converts this into an empty catalog.
        """
        body = await self._get("/internal/model-catalog", {"user_id": user_id})
        models = body.get("models")
        if not isinstance(models, list):
            raise GatewayError("gateway returned no model list")
        return [str(model) for model in models]

    async def user_status(self, user_id: str) -> dict[str, Any]:
        """Report whether a user exists, may sign in, and with what role.

        Args:
            user_id: The gateway's user identifier.

        Returns:
            The gateway's status record.

        Raises:
            GatewayError: If the status cannot be read.
        """
        return await self._get(f"/internal/users/{user_id}/status")

    async def mcp_registry(self) -> list[dict[str, Any]]:
        """List the MCP servers this deployment offers.

        Names and display metadata only — the gateway never returns a server's
        address or credential, and this service never needs them: a sandbox
        reaches MCP through the gateway's proxy.

        Returns:
            One record per configured server.

        Raises:
            GatewayError: If the registry cannot be read.
        """
        body = await self._get("/internal/mcp-registry")
        servers = body.get("servers")
        if not isinstance(servers, list):
            raise GatewayError("gateway returned no server list")
        return [server for server in servers if isinstance(server, dict)]
