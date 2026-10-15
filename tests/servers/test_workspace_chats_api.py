"""``POST /v1/agent/jobs/{job_id}/workspace-chats``: a new chat in an existing workspace.

The route runs a new task's checks on the conversation's own choices and takes
everything the workspace fixed from the chat it joins; the store decides under
the group's lock whether the workspace takes another chat, and each of its
refusals reaches the owner as a stable 409.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from cloud_agent.api import jobs
from cloud_agent.api._job_views import _job_response
from cloud_agent.api._owner_auth import require_agent_owner
from cloud_agent.api.deps import get_agent_app_credentials, get_gateway_client, get_storage
from cloud_agent.application import workspace_chats
from cloud_agent.domain.requests import request_fingerprint
from cloud_agent.domain.ssh_servers import SshServerUnavailable
from cloud_agent.entitlement import RepoNotAllowed
from cloud_agent.schemas import BASE_REF_METADATA_KEY, AgentWorkspaceChatCreate

_NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)
_SANDBOX = {
    "shared_live": True,
    "sandbox_id": "asbx_shared",
    "origin_thread_id": "athr_origin",
    "runtime_id": "claude-code",
    "workspace_chat_capable": True,
}


def _job(**overrides: Any) -> dict[str, Any]:
    job = {
        "id": "ajob_origin",
        "thread_id": "athr_origin",
        "user_id": "alice",
        "repo": "example-org/example-repo",
        "task_prompt": "set the project up",
        "runtime": "claude-code",
        "model": "glm-5.1",
        "base_sha": "a" * 40,
        "state": "succeeded",
        "cancel_requested": False,
        "current_attempt_id": None,
        "published_pr_url": None,
        "detail": None,
        "metadata": {BASE_REF_METADATA_KEY: "dev"},
        "created_at": _NOW,
        "updated_at": _NOW,
        "thread_sandbox": dict(_SANDBOX),
    }
    job.update(overrides)
    return job


@pytest.fixture
def app(monkeypatch):
    application = FastAPI()
    application.include_router(jobs.router)
    commands = AsyncMock()
    commands.replayed_submission.return_value = None
    commands.create_workspace_chat.return_value = _job(
        id="ajob_joined", thread_id="athr_joined", task_prompt="check the README"
    )
    queries = AsyncMock()
    queries.get_job.return_value = _job()
    store = SimpleNamespace(
        conversation_commands=commands,
        conversation_queries=queries,
        mcp=AsyncMock(),
        skills=AsyncMock(),
        attachments=AsyncMock(),
        source_control=AsyncMock(),
    )
    application.dependency_overrides[require_agent_owner] = lambda: {"user_id": "alice"}
    application.dependency_overrides[get_storage] = lambda: store
    application.dependency_overrides[get_agent_app_credentials] = lambda: None
    application.dependency_overrides[get_gateway_client] = lambda: object()
    checks = SimpleNamespace(model=AsyncMock(return_value="glm-5.1-canonical"))
    checks.effort = AsyncMock(side_effect=lambda effort, **_kwargs: effort)
    checks.entitled = AsyncMock()
    monkeypatch.setattr(workspace_chats, "_require_resolvable_model", checks.model)
    monkeypatch.setattr(workspace_chats, "_require_offered_effort", checks.effort)
    monkeypatch.setattr(workspace_chats, "require_entitled_repo", checks.entitled)
    monkeypatch.setattr(workspace_chats, "_resolve_mcp_bindings", AsyncMock(return_value=[]))
    monkeypatch.setattr(workspace_chats, "_resolve_skill_selection", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        workspace_chats, "_resolve_attachment_selection", AsyncMock(return_value=None)
    )
    application.state.store = store
    application.state.checks = checks
    return application


async def _post(app: FastAPI, body: dict[str, Any], headers: dict[str, str] | None = None):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        return await client.post(
            "/v1/agent/jobs/ajob_origin/workspace-chats", json=body, headers=headers or {}
        )


_BODY = {"task_prompt": "  check the README  ", "model": "glm-5.1", "reasoning_effort": "high"}


async def test_the_new_chat_takes_the_workspace_and_its_own_choices(app):
    response = await _post(
        app,
        {
            **_BODY,
            "metadata": {"source": "caller", "requires": ["x"], BASE_REF_METADATA_KEY: "main"},
        },
        headers={"Idempotency-Key": "send-1"},
    )

    assert response.status_code == 201, response.text
    assert response.json()["id"] == "ajob_joined"
    kwargs = app.state.store.conversation_commands.create_workspace_chat.await_args.kwargs
    assert kwargs["source_job_id"] == "ajob_origin"
    assert kwargs["user_id"] == "alice"
    assert kwargs["task_prompt"] == "check the README"
    assert kwargs["model"] == "glm-5.1-canonical"
    assert kwargs["reasoning_effort"] == "high"
    # The workspace's branch, never the caller's; reserved keys never pass.
    assert kwargs["metadata"] == {"source": "caller", BASE_REF_METADATA_KEY: "dev"}
    assert kwargs["client_request_id"] == "send-1"
    body = AgentWorkspaceChatCreate.model_validate(
        {
            **_BODY,
            "metadata": {"source": "caller", "requires": ["x"], BASE_REF_METADATA_KEY: "main"},
        }
    )
    assert kwargs["client_request_fingerprint"] == request_fingerprint(
        "workspace-chat", {"source_job_id": "ajob_origin", **body.model_dump(mode="json")}
    )
    # Effort is judged against the workspace's runtime and the resolved model.
    assert app.state.checks.effort.await_args.kwargs["runtime"] == "claude-code"
    assert app.state.checks.effort.await_args.kwargs["model"] == "glm-5.1-canonical"
    assert app.state.checks.entitled.await_args.args[:2] == ("example-org/example-repo", "alice")


@pytest.mark.parametrize("field", ["repo", "runtime", "base_ref", "environment_id", "plugins"])
async def test_what_the_workspace_fixed_cannot_be_chosen(app, field):
    response = await _post(app, {**_BODY, field: "anything"})

    assert response.status_code == 422
    app.state.store.conversation_commands.create_workspace_chat.assert_not_called()


@pytest.mark.parametrize(
    ("error", "error_type"),
    [
        ("workspace_unavailable", "workspace_unavailable"),
        ("workspace_not_ready", "workspace_not_ready"),
        ("workspace_chat_unavailable", "workspace_chat_unavailable"),
        ("resource_admission_blocked", "workspace_chat_unavailable"),
        ("resident_capacity_exhausted", "resident_capacity_exhausted"),
        ("session_archived", "session_archived"),
        ("client_request_conflict", "idempotency_conflict"),
    ],
)
async def test_every_store_refusal_is_a_stable_conflict(app, error, error_type):
    app.state.store.conversation_commands.create_workspace_chat.return_value = {"error": error}

    response = await _post(app, _BODY)

    assert response.status_code == 409
    assert response.json()["detail"]["error"]["type"] == error_type


async def test_an_archived_chat_is_refused_before_any_model_lookup(app):
    app.state.store.conversation_queries.get_job.return_value = _job(archived_at=_NOW)

    response = await _post(app, _BODY)

    assert response.status_code == 409
    assert response.json()["detail"]["error"]["type"] == "session_archived"
    app.state.checks.model.assert_not_called()


@pytest.mark.parametrize(
    ("conversation_state", "error_type"),
    [
        # Its first turn has not reached the sandbox it is creating.
        ("queued", "workspace_not_ready"),
        ("running", "workspace_not_ready"),
        # Nothing is on its way: a chat from before retained workspaces.
        ("idle", "workspace_unavailable"),
        ("failed", "workspace_unavailable"),
    ],
)
async def test_a_chat_with_no_retained_workspace_has_nothing_to_join(
    app, conversation_state, error_type
):
    app.state.store.conversation_queries.get_job.return_value = _job(
        thread_sandbox=None, conversation_state=conversation_state
    )

    response = await _post(app, _BODY)

    assert response.status_code == 409
    assert response.json()["detail"]["error"]["type"] == error_type
    app.state.store.conversation_commands.create_workspace_chat.assert_not_called()


async def test_another_owners_chat_does_not_exist(app):
    app.state.store.conversation_queries.get_job.return_value = _job(user_id="mallory")

    response = await _post(app, _BODY)

    assert response.status_code == 404
    app.state.store.conversation_commands.create_workspace_chat.assert_not_called()


async def test_a_repository_the_owner_lost_is_refused(app):
    app.state.checks.entitled.side_effect = RepoNotAllowed("no longer allowed")

    response = await _post(app, _BODY)

    assert response.status_code == 403
    assert response.json()["detail"]["error"]["type"] == "repo_not_allowed"
    app.state.store.conversation_commands.create_workspace_chat.assert_not_called()


async def test_ssh_servers_are_the_new_chats_own_choice(app):
    server = "0b9c7a4e-5d1f-4c8a-9e2b-3f6d8a1c2e4f"

    accepted = await _post(app, {**_BODY, "ssh_server_ids": [server]})
    kwargs = app.state.store.conversation_commands.create_workspace_chat.await_args.kwargs
    app.state.store.conversation_commands.create_workspace_chat.side_effect = SshServerUnavailable(
        [server]
    )
    refused = await _post(app, {**_BODY, "ssh_server_ids": [server]})

    assert accepted.status_code == 201
    assert kwargs["ssh_server_ids"] == [server]
    assert refused.status_code == 400
    assert server in refused.json()["detail"]["error"]["message"]


async def test_a_resend_returns_the_first_chat_without_checking_again(app):
    app.state.store.conversation_commands.replayed_submission.return_value = _job(
        id="ajob_joined", thread_id="athr_joined"
    )

    response = await _post(app, _BODY, headers={"Idempotency-Key": "send-1"})

    assert response.status_code == 201
    assert response.json()["id"] == "ajob_joined"
    app.state.checks.model.assert_not_called()
    app.state.store.conversation_commands.create_workspace_chat.assert_not_called()


@pytest.mark.parametrize(
    ("sandbox", "archived", "available"),
    [
        (_SANDBOX, None, True),
        ({**_SANDBOX, "workspace_chat_capable": False}, None, False),
        (_SANDBOX, _NOW, False),
        (None, None, False),
    ],
)
def test_the_page_is_offered_the_action_only_when_the_store_would_take_it(
    sandbox, archived, available
):
    view = _job_response(_job(thread_sandbox=sandbox, archived_at=archived))

    assert view.workspace_chat_available is available
