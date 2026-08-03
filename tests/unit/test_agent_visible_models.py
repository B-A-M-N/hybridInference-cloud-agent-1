"""The catalog, now asked for rather than computed.

The source's four tests walked a fake route registry — role gates, alias
resolution, unpublished/disabled/embedding exclusion, visibility-resolver
overrides. **None of that behaviour lives here after E4**; it stays in the
gateway, which still owns the registry, and is covered there by
``tests/unit/test_internal_lookups.py`` (see MIGRATION.md's manifest notes for
the case-by-case record).

What this service can still get wrong is different, and is what these test:
whether it faithfully reports what the gateway said, and whether it fails
*closed* when the gateway cannot answer.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from cloud_agent.gateway import GatewayClient, GatewayError
from cloud_agent.visible_models import agent_model_resolvable, agent_visible_models

BASE = "https://gateway.example"
TOKEN = "dispatch-token"
CATALOG = ["glm-5.1", "qwen3.6-35b", "kimi-k2"]


def _client(handler) -> GatewayClient:
    transport = httpx.MockTransport(handler)
    return GatewayClient(BASE, TOKEN, client=httpx.AsyncClient(transport=transport))


def _serves(payload: dict[str, Any], status_code: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload)

    return handler


# ---------------------------------------------------------------------------
# Reporting what the gateway said
# ---------------------------------------------------------------------------


async def test_the_catalog_is_whatever_the_gateway_returned() -> None:
    gateway = _client(_serves({"models": CATALOG}))
    assert await agent_visible_models(gateway, user_id="user_1") == CATALOG


async def test_the_request_carries_the_user_and_the_service_credential() -> None:
    """The endpoint resolves per user, so the user has to be on the wire.

    And the credential is a *service* one: it travels here and never toward a
    sandbox or a browser.
    """
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"models": CATALOG})

    await agent_visible_models(_client(handler), user_id="user_42")
    assert "user_id=user_42" in seen["url"]
    assert "/internal/model-catalog" in seen["url"]
    assert seen["auth"] == f"Bearer {TOKEN}"


async def test_the_catalog_is_not_the_public_models_route() -> None:
    """`/v1/models` cannot authenticate a service credential.

    It would answer with the anonymous catalog, silently narrowing every
    non-free user to what a stranger sees — a degrade with no error attached.
    """
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json={"models": CATALOG})

    await agent_visible_models(_client(handler), user_id="user_1")
    assert seen["path"] != "/v1/models"


async def test_resolvable_accepts_a_listed_model_and_refuses_an_unlisted_one() -> None:
    gateway = _client(_serves({"models": CATALOG}))
    assert await agent_model_resolvable("glm-5.1", gateway, user_id="user_1") is True
    assert await agent_model_resolvable("gpt-5-secret", gateway, user_id="user_1") is False


# ---------------------------------------------------------------------------
# Failing closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "handler"),
    [
        ("gateway 500", _serves({}, 500)),
        ("gateway 403", _serves({}, 403)),
        ("no models key", _serves({"role": "pro"})),
        ("models not a list", _serves({"models": "everything"})),
    ],
)
async def test_an_unusable_answer_yields_no_models_not_every_model(name, handler) -> None:
    """The direction of this default is the whole point.

    An outage that widened what a job may call would do so at exactly the
    moment nobody is watching. An empty picker is visibly broken; a picker
    offering models the user cannot call produces a job that dies at its first
    request, far from the cause.
    """
    assert await agent_visible_models(_client(handler), user_id="user_1") == []


async def test_an_unreachable_gateway_yields_no_models() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    assert await agent_visible_models(_client(handler), user_id="user_1") == []


async def test_resolvable_refuses_everything_when_the_catalog_is_unavailable() -> None:
    """Fail-closed has to hold on the create-time check too.

    Otherwise an outage lets any model name through job creation, and the
    failure surfaces an attempt later.
    """
    gateway = _client(_serves({}, 503))
    assert await agent_model_resolvable("glm-5.1", gateway, user_id="user_1") is False


async def test_an_unconfigured_deployment_raises_rather_than_guessing() -> None:
    """At the client level the error is loud; only the caller softens it."""
    with pytest.raises(GatewayError):
        await GatewayClient("", "").model_catalog("user_1")
