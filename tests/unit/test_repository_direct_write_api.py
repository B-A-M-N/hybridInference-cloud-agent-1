"""Repository direct-write settings require a linked GitHub repository admin."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cloud_agent.api import source_control
from cloud_agent.api._owner_auth import require_agent_owner
from cloud_agent.api.deps import get_agent_app_credentials, get_storage
from cloud_agent.ports import McpConnectionRepository, SourceControlRepository
from cloud_agent.source_control import SourceControlCipher
from cloud_agent.storage.assembly import Storage

_USER_ID = "platform-user-17"
_REPO = "Example/Project"
_REPOSITORY_ID = 8051
_INSTALLATION_ID = 300


class _GitHub:
    """Synthetic verified GitHub API responses for this route's integration tests."""

    cipher = SourceControlCipher("test-user-token-key")
    direct_write_cipher = SourceControlCipher(
        "test-user-token-key", domain=b"hybridinference/direct-write-token/v1"
    )

    def __init__(self, *, admin: bool = True, revoke_error: Exception | None = None) -> None:
        self.admin = admin
        self.revoke_installation_token = AsyncMock(side_effect=revoke_error)

    async def installation_id_for(self, repo: str) -> int:
        assert repo.casefold() == _REPO.casefold()
        return _INSTALLATION_ID

    async def repositories_for_user(
        self, user_token: str, installation_id: int
    ) -> list[dict[str, Any]]:
        assert user_token == "verified-user-token"
        assert installation_id == _INSTALLATION_ID
        return [
            {
                "id": _REPOSITORY_ID,
                "full_name": _REPO,
                "permissions": {"admin": self.admin, "push": True},
            }
        ]


def _make_client(repository: AsyncMock, github: _GitHub) -> tuple[FastAPI, AsyncClient]:
    storage = Storage(Mock())
    storage.source_control = repository
    storage.mcp = AsyncMock(spec=McpConnectionRepository)
    app = FastAPI()
    app.include_router(source_control.router)
    app.dependency_overrides[require_agent_owner] = lambda: {"user_id": _USER_ID}
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_agent_app_credentials] = lambda: github
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    return app, client


def _repository(*, installation_connected: bool = True) -> AsyncMock:
    repository = AsyncMock(spec=SourceControlRepository)
    repository.list_repo_grants.return_value = (
        [{"installation_id": _INSTALLATION_ID}] if installation_connected else []
    )
    repository.get_github_account.return_value = {
        "access_token_ciphertext": _GitHub.cipher.encrypt("verified-user-token"),
        "access_expires_at": datetime.now(UTC) + timedelta(hours=1),
    }
    repository.get_direct_write_policy.return_value = {
        "github_repository_id": _REPOSITORY_ID,
        "repository_full_name": _REPO,
        "enabled": False,
        "revision": 0,
        "updated_by": None,
        "updated_at": None,
    }
    repository.list_direct_write_tokens.return_value = []
    return repository


@pytest.mark.asyncio
async def test_policy_get_uses_github_identity_and_defaults_disabled() -> None:
    repository = _repository()
    github = _GitHub()
    _app, client = _make_client(repository, github)
    try:
        response = await client.get("/v1/agent/repositories/example/project/direct-write")
    finally:
        await client.aclose()

    assert response.status_code == 200
    assert response.json() == {
        "github_repository_id": _REPOSITORY_ID,
        "repository": _REPO,
        "enabled": False,
        "revision": 0,
        "updated_by": None,
        "updated_at": None,
        "revocation_pending": 0,
    }
    repository.get_direct_write_policy.assert_awaited_once_with(
        github_repository_id=_REPOSITORY_ID, repository_full_name=_REPO
    )


@pytest.mark.asyncio
async def test_app_coverage_without_linked_users_admin_permission_cannot_enable() -> None:
    repository = _repository()
    github = _GitHub(admin=False)
    _app, client = _make_client(repository, github)
    try:
        response = await client.put(
            "/v1/agent/repositories/example/project/direct-write",
            json={"enabled": True, "expected_revision": 0},
        )
    finally:
        await client.aclose()

    assert response.status_code == 403
    assert response.json()["detail"]["error"]["type"] == "repository_admin_required"
    repository.set_direct_write_policy.assert_not_awaited()


@pytest.mark.asyncio
async def test_connected_app_installation_is_not_enough_to_enable_policy() -> None:
    repository = _repository(installation_connected=False)
    github = _GitHub()
    _app, client = _make_client(repository, github)
    try:
        response = await client.put(
            "/v1/agent/repositories/example/project/direct-write",
            json={"enabled": True, "expected_revision": 0},
        )
    finally:
        await client.aclose()

    assert response.status_code == 403
    assert response.json()["detail"]["error"]["type"] == "repository_not_connected"
    repository.set_direct_write_policy.assert_not_awaited()


@pytest.mark.asyncio
async def test_disable_returns_pending_revocation_count_without_exposing_tokens() -> None:
    repository = _repository()
    repository.set_direct_write_policy.return_value = {
        "policy": {
            "github_repository_id": _REPOSITORY_ID,
            "repository_full_name": _REPO,
            "enabled": False,
            "revision": 3,
            "updated_by": _USER_ID,
            "updated_at": None,
        },
        "tokens": [
            {
                "token_hash": "hash-1",
                "token_ciphertext": _GitHub.direct_write_cipher.encrypt("write-secret"),
            }
        ],
    }
    github = _GitHub(revoke_error=TimeoutError("GitHub unavailable"))
    _app, client = _make_client(repository, github)
    try:
        response = await client.put(
            "/v1/agent/repositories/example/project/direct-write",
            json={"enabled": False, "expected_revision": 2},
        )
    finally:
        await client.aclose()

    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert response.json()["revision"] == 3
    assert response.json()["revocation_pending"] == 1
    assert "write-secret" not in response.text
    github.revoke_installation_token.assert_awaited_once_with("write-secret")
    repository.forget_direct_write_token.assert_not_awaited()


@pytest.mark.asyncio
async def test_reenable_revokes_tokens_from_the_previous_policy_revision() -> None:
    repository = _repository()
    repository.set_direct_write_policy.return_value = {
        "policy": {
            "github_repository_id": _REPOSITORY_ID,
            "repository_full_name": _REPO,
            "enabled": True,
            "revision": 4,
            "updated_by": _USER_ID,
            "updated_at": None,
        },
        "tokens": [
            {
                "token_hash": "old-hash",
                "token_ciphertext": _GitHub.direct_write_cipher.encrypt("old-write-token"),
            }
        ],
    }
    github = _GitHub()
    _app, client = _make_client(repository, github)
    try:
        response = await client.put(
            "/v1/agent/repositories/example/project/direct-write",
            json={"enabled": True, "expected_revision": 3},
        )
    finally:
        await client.aclose()

    assert response.status_code == 200
    assert response.json()["enabled"] is True
    github.revoke_installation_token.assert_awaited_once_with("old-write-token")
    repository.forget_direct_write_token.assert_awaited_once_with(token_hash="old-hash")
