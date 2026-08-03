"""The gateway client — every question this service used to answer by import.

Tested at this level because three consumers depend on the same failure
behaviour: E4's catalog, E7/E8's user status, E6's MCP registry. If the client
ever failed *open* — treating an unreachable gateway as permission — each of
them would inherit that quietly.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from cloud_agent.gateway import ENV_BASE_URL, ENV_DISPATCH_TOKEN, GatewayClient, GatewayError

BASE = "https://gateway.example"
TOKEN = "dispatch-token"


def _client(handler) -> GatewayClient:
    return GatewayClient(
        BASE, TOKEN, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


def _serves(payload: Any, status_code: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload)

    return handler


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_configured_requires_both_halves() -> None:
    assert GatewayClient(BASE, TOKEN).configured is True
    assert GatewayClient(BASE, "").configured is False
    assert GatewayClient("", TOKEN).configured is False


def test_configuration_falls_back_to_the_environment(monkeypatch) -> None:
    monkeypatch.setenv(ENV_BASE_URL, BASE)
    monkeypatch.setenv(ENV_DISPATCH_TOKEN, TOKEN)
    assert GatewayClient().configured is True


async def test_a_trailing_slash_does_not_produce_a_double_slash() -> None:
    """A `//internal/...` path is a 404 that reads like a missing endpoint."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json={"servers": []})

    client = GatewayClient(
        f"{BASE}/", TOKEN, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    await client.mcp_registry()
    assert seen["path"] == "/internal/mcp-registry"


# ---------------------------------------------------------------------------
# Every method fails closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 500, 503])
async def test_a_non_200_raises(status_code: int) -> None:
    with pytest.raises(GatewayError):
        await _client(_serves({}, status_code)).user_status("user_1")


async def test_unparsable_json_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    with pytest.raises(GatewayError):
        await _client(handler).user_status("user_1")


async def test_a_non_object_body_raises() -> None:
    """A list where an object belongs is a contract change, not a value."""
    with pytest.raises(GatewayError):
        await _client(_serves([1, 2, 3])).user_status("user_1")


async def test_a_transport_failure_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    with pytest.raises(GatewayError):
        await _client(handler).model_catalog("user_1")


async def test_an_unconfigured_client_raises_before_any_request() -> None:
    with pytest.raises(GatewayError, match=ENV_BASE_URL):
        await GatewayClient("", "").user_status("user_1")


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


async def test_user_status_returns_the_record_verbatim() -> None:
    record = {"user_id": "user_1", "exists": True, "active": True, "role": "pro"}
    assert await _client(_serves(record)).user_status("user_1") == record


async def test_mcp_registry_drops_non_object_entries() -> None:
    """A malformed entry must not become a server name the picker offers."""
    servers = await _client(_serves({"servers": [{"name": "github"}, "junk", 7]})).mcp_registry()
    assert servers == [{"name": "github"}]


async def test_mcp_registry_raises_when_the_list_is_missing() -> None:
    with pytest.raises(GatewayError):
        await _client(_serves({"oops": True})).mcp_registry()
