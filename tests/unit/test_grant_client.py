"""Asking the gateway for capability.

The interesting assertions are about the three calls' different attitudes to
failure — mint and renew must not be papered over, revoke must be — and about
using the *effective* scope the gateway returns rather than what was asked for.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from cloud_agent.gateway import GatewayError
from cloud_agent.grants import GRANT_TTL_S, GrantClient

BASE = "https://gateway.example"
TOKEN = "dispatch-token"

GRANT = {
    "grant_id": "agr_1",
    "user_id": "user_1",
    "external_job_id": "job_1",
    "external_attempt_id": "1",
    "allowed_models": ["glm-5.1"],
    "allowed_mcp": ["deepwiki"],
    "expires_at": "2026-08-04T12:00:00+00:00",
    "revoked_at": None,
    "token": "agr.payload.signature",
}


def _client(handler) -> GrantClient:
    return GrantClient(
        BASE, TOKEN, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


def _serves(payload: Any, status_code: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload)

    return handler


# ---------------------------------------------------------------------------
# Minting
# ---------------------------------------------------------------------------


async def test_mint_sends_the_scope_and_returns_the_grant() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json=GRANT)

    grant = await _client(handler).mint(
        user_id="user_1", job_id="job_1", attempt_id="1", models=["glm-5.1", "kimi-k2"]
    )
    assert seen["path"] == "/internal/agent-grants"
    assert seen["body"]["allowed_models"] == ["glm-5.1", "kimi-k2"]
    assert seen["auth"] == f"Bearer {TOKEN}"
    assert grant["token"] == "agr.payload.signature"


async def test_the_returned_scope_may_be_narrower_than_the_request() -> None:
    """Use what came back, not what was sent.

    The gateway clamps models to what the role may reach. A job built around a
    model the grant does not carry fails at its first call, far from here.
    """
    grant = await _client(_serves(GRANT)).mint(
        user_id="user_1", job_id="job_1", attempt_id="1", models=["glm-5.1", "kimi-k2"]
    )
    assert grant["allowed_models"] == ["glm-5.1"]


async def test_omitted_scope_is_not_sent_as_null() -> None:
    """`None` means "the gateway decides", which is absence, not a null value."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=GRANT)

    await _client(handler).mint(user_id="user_1", job_id="job_1", attempt_id="1", ttl_seconds=None)
    assert "allowed_models" not in seen["body"]
    assert "allowed_mcp" not in seen["body"]
    assert "ttl_seconds" not in seen["body"]


async def test_a_lifetime_is_always_asked_for() -> None:
    """The caller does not pass one, so the default is what defends the loop.

    Left to the gateway's own default, the grant's lifetime would be a number
    this service does not know and cannot keep in step with the pass that
    renews it — and the failure would be a sandbox losing its capability
    mid-run for no visible reason.
    """
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=GRANT)

    await _client(handler).mint(user_id="user_1", job_id="job_1", attempt_id="1")
    assert seen["body"]["ttl_seconds"] == GRANT_TTL_S


async def test_an_attempt_id_is_sent_as_a_string() -> None:
    """The store's attempt ids are integers; the contract's are strings."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=GRANT)

    await _client(handler).mint(user_id="user_1", job_id="job_1", attempt_id=7)
    assert seen["body"]["external_attempt_id"] == "7"


async def test_a_refused_mint_surfaces_the_gateway_reason() -> None:
    """ "Unknown MCP server" and "that user cannot be granted" differ.

    Whoever created the job needs to be able to tell them apart, so the
    gateway's message travels rather than being flattened into a status code.
    """
    payload = {"error": {"type": "unknown_mcp_server", "message": "unknown MCP server(s): ghost"}}
    with pytest.raises(GatewayError, match="ghost"):
        await _client(_serves(payload, 400)).mint(
            user_id="user_1", job_id="job_1", attempt_id="1", mcp_servers=["ghost"]
        )


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 500, 503])
async def test_mint_raises_on_any_refusal(status_code: int) -> None:
    with pytest.raises(GatewayError):
        await _client(_serves({}, status_code)).mint(
            user_id="user_1", job_id="job_1", attempt_id="1"
        )


async def test_mint_raises_when_the_gateway_is_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(GatewayError):
        await _client(handler).mint(user_id="user_1", job_id="job_1", attempt_id="1")


async def test_an_unconfigured_client_raises_before_any_request() -> None:
    with pytest.raises(GatewayError):
        await GrantClient("", "").mint(user_id="user_1", job_id="job_1", attempt_id="1")


# ---------------------------------------------------------------------------
# Renewal
# ---------------------------------------------------------------------------


async def test_renew_targets_the_grant() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json=GRANT)

    await _client(handler).renew("agr_1")
    assert seen["path"] == "/internal/agent-grants/agr_1/renew"


async def test_a_refused_renewal_raises_rather_than_pretending() -> None:
    """A renewal that failed and looked fine is a grant about to expire mid-run.

    The caller has to know, so it can fail the attempt deliberately instead of
    discovering it when the sandbox's next model call is refused.
    """
    with pytest.raises(GatewayError):
        await _client(_serves({}, 404)).renew("agr_1")


async def test_a_refusal_carries_why_and_not_only_that() -> None:
    """The renewal loop abandons running attempts on a refusal, so it must be
    able to tell "no such grant" from "your dispatch token is wrong" — and at
    this gateway both are 404-or-401 with the reason only in the body."""
    payload = {"error": {"type": "grant_not_live", "message": "That grant is revoked."}}
    with pytest.raises(GatewayError) as raised:
        await _client(_serves(payload, 404)).renew("agr_1")

    assert raised.value.status_code == 404
    assert raised.value.error_type == "grant_not_live"
    assert raised.value.definitive is True


async def test_an_unreachable_gateway_carries_no_verdict() -> None:
    """No answer means no status and no type. A caller that reads a missing
    status as a refusal cannot help but treat downtime as a verdict."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(GatewayError) as raised:
        await _client(handler).renew("agr_1")

    assert raised.value.status_code is None
    assert raised.value.error_type is None
    assert raised.value.definitive is False


@pytest.mark.parametrize("status_code", [429, 408, 500, 503])
async def test_ask_again_answers_are_not_definitive(status_code: int) -> None:
    """Backpressure and a sick gateway are requests for patience, not verdicts
    on the request — the caller may retry, and giving up on them would give up
    on every grant at once the moment the gateway had a bad minute."""
    with pytest.raises(GatewayError) as raised:
        await _client(_serves({}, status_code)).renew("agr_1")

    assert raised.value.definitive is False


async def test_a_refusal_without_a_readable_body_still_carries_its_status() -> None:
    """A proxy's HTML 502, or an empty 401: no type to read, and the absence
    must not be mistaken for a type this service recognises."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="<html>not authorized</html>")

    with pytest.raises(GatewayError) as raised:
        await _client(handler).renew("agr_1")

    assert raised.value.status_code == 401
    assert raised.value.error_type is None


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------


async def test_revoke_reports_success() -> None:
    assert await _client(_serves({"revoked": True})).revoke("agr_1") is True


@pytest.mark.parametrize("status_code", [404, 500, 503])
async def test_a_failed_revoke_returns_false_instead_of_raising(status_code: int) -> None:
    """Revoke is an acceleration of the TTL, not the mechanism.

    It runs on cleanup paths that have other work to finish, so a failure must
    not take the caller down — the grant dies of its TTL regardless, which is
    the whole reason revocation needs no durable outbox behind it.
    """
    assert await _client(_serves({}, status_code)).revoke("agr_1") is False


async def test_an_unreachable_gateway_does_not_break_revocation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    assert await _client(handler).revoke("agr_1") is False


async def test_revoke_on_an_unconfigured_client_does_not_raise() -> None:
    assert await GrantClient("", "").revoke("agr_1") is False
