"""Postgres-backed verification of direct-write claim credential lifecycle."""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

import pytest
import pytest_asyncio
from tests.support.migrations import apply_migrations

from cloud_agent.api.worker import _direct_write_token_for_claim
from cloud_agent.source_control import SourceControlCipher
from cloud_agent.storage.repositories.source_control import PostgresSourceControlRepository

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]
_ALLOWED_TEST_DB_PATTERN = "_test_"


def _test_db_config() -> dict[str, Any]:
    """Resolve and safety-check the shared test database configuration."""
    base_db_name = os.getenv("TEST_DB_NAME", "hybridinference_test_db")
    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "master")
    test_db_name = base_db_name if worker_id == "master" else f"{base_db_name}_{worker_id}"
    if _ALLOWED_TEST_DB_PATTERN not in test_db_name:
        pytest.fail(f"SAFETY: TEST_DB_NAME='{test_db_name}' is not a test database.")
    return {
        "host": os.getenv("TEST_DB_HOST", "localhost"),
        "port": int(os.getenv("TEST_DB_PORT", "5432")),
        "database": test_db_name,
        "user": os.getenv("TEST_DB_USER", "postgres"),
        "password": os.getenv("TEST_DB_PASSWORD", "postgres"),
    }


@pytest_asyncio.fixture
async def pool():
    """Connect only to the explicitly named test database and apply migrations."""
    import asyncpg

    db_config = _test_db_config()
    try:
        pool = await asyncpg.create_pool(**db_config, min_size=1, max_size=5)
    except Exception as exc:
        pytest.skip(f"PostgreSQL not available: {exc}")
        return
    apply_migrations(db_config)
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def store(pool):
    """Use the production repository implementation against Postgres."""
    return PostgresSourceControlRepository(pool)


async def test_concurrent_direct_write_claims_get_unique_tokens_that_rotate_on_policy_revision(
    store: PostgresSourceControlRepository,
    pool,
) -> None:
    """Concurrent claims get separate revocable tokens; revisions revoke and rotate them."""
    repository_id = uuid.uuid4().int & ((1 << 62) - 1)
    repository_name = "octocat/concurrent-project"

    class PerClaimCredentials:
        def __init__(self) -> None:
            self.direct_write_cipher = SourceControlCipher("test-direct-write-cache-key")
            self.minted: list[str] = []
            self.revoked: list[str] = []

        async def repository_identity(self, repo: str) -> dict[str, Any]:
            assert repo == repository_name
            return {"repository_id": repository_id, "full_name": repository_name}

        async def token_for(self, repo: str, **kwargs: Any) -> str:
            assert repo == repository_name
            assert kwargs == {
                "permissions": {"contents": "write", "pull_requests": "write"},
                "repository_scoped": True,
                "cache": False,
            }
            token = f"installation-token-{len(self.minted) + 1}"
            self.minted.append(token)
            return token

        async def revoke_installation_token(self, token: str) -> None:
            self.revoked.append(token)

    credentials = PerClaimCredentials()
    try:
        await store.get_direct_write_policy(
            github_repository_id=repository_id,
            repository_full_name=repository_name,
        )
        enabled = await store.set_direct_write_policy(
            github_repository_id=repository_id,
            repository_full_name=repository_name,
            enabled=True,
            expected_revision=0,
            actor_id="github-user-42",
            admin_ip="203.0.113.42",
        )
        assert enabled is not None and enabled["policy"]["revision"] == 1

        first, second = await asyncio.gather(
            _direct_write_token_for_claim(
                repository_name,
                store=store,
                app_credentials=credentials,
                job_id="concurrent-claim-1",
            ),
            _direct_write_token_for_claim(
                repository_name,
                store=store,
                app_credentials=credentials,
                job_id="concurrent-claim-2",
            ),
        )
        assert first[1] == second[1] == repository_name
        assert first[0] and second[0] and first[0] != second[0]
        registered = await store.list_direct_write_tokens(github_repository_id=repository_id)
        assert len(registered) == 2
        assert {
            credentials.direct_write_cipher.decrypt(token["token_ciphertext"])
            for token in registered
        } == {first[0], second[0]}

        disabled = await store.set_direct_write_policy(
            github_repository_id=repository_id,
            repository_full_name=repository_name,
            enabled=False,
            expected_revision=1,
            actor_id="github-user-42",
            admin_ip="203.0.113.42",
        )
        assert disabled is not None
        assert disabled["policy"]["revision"] == 2
        assert len(disabled["tokens"]) == 2
        for token in disabled["tokens"]:
            plaintext = credentials.direct_write_cipher.decrypt(token["token_ciphertext"])
            await credentials.revoke_installation_token(plaintext)
            await store.forget_direct_write_token(token_hash=token["token_hash"])
        assert set(credentials.revoked) == {first[0], second[0]}
        assert await store.list_direct_write_tokens(github_repository_id=repository_id) == []

        reenabled = await store.set_direct_write_policy(
            github_repository_id=repository_id,
            repository_full_name=repository_name,
            enabled=True,
            expected_revision=2,
            actor_id="github-user-42",
            admin_ip="203.0.113.42",
        )
        assert reenabled is not None and reenabled["policy"]["revision"] == 3
        rotated, rotated_repo = await _direct_write_token_for_claim(
            repository_name,
            store=store,
            app_credentials=credentials,
            job_id="reenabled-claim",
        )
        assert (rotated, rotated_repo) == ("installation-token-3", repository_name)
        rows = await store.list_direct_write_tokens(github_repository_id=repository_id)
        assert len(rows) == 1
        assert credentials.direct_write_cipher.decrypt(rows[0]["token_ciphertext"]) == rotated
        async with pool.acquire() as conn:
            revision = await conn.fetchval(
                """
                SELECT policy_revision FROM agent_repository_direct_write_tokens
                WHERE token_hash = $1
                """,
                rows[0]["token_hash"],
            )
        assert revision == 3
    finally:
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM agent_repository_direct_write_policy WHERE github_repository_id = $1",
                repository_id,
            )
            await conn.execute(
                "DELETE FROM agent_admin_audit_log WHERE details->>'github_repository_id' = $1",
                str(repository_id),
            )


async def test_direct_write_mint_is_revoked_when_postgres_policy_changes_mid_mint(
    store: PostgresSourceControlRepository,
    pool,
) -> None:
    """A real policy revision race cannot register or return the stale token."""
    from cloud_agent.api.worker import DirectWritePolicyUnavailable

    repository_id = uuid.uuid4().int & ((1 << 62) - 1)
    repository_name = "octocat/revision-race-project"
    mint_started = asyncio.Event()
    release_mint = asyncio.Event()

    class RacingCredentials:
        def __init__(self) -> None:
            self.direct_write_cipher = SourceControlCipher("test-direct-write-race-key")
            self.revoked: list[str] = []

        async def repository_identity(self, repo: str) -> dict[str, Any]:
            assert repo == repository_name
            return {"repository_id": repository_id, "full_name": repository_name}

        async def token_for(self, repo: str, **kwargs: Any) -> str:
            assert repo == repository_name
            assert kwargs["cache"] is False
            mint_started.set()
            await release_mint.wait()
            return "stale-installation-token"

        async def revoke_installation_token(self, token: str) -> None:
            self.revoked.append(token)

    credentials = RacingCredentials()
    try:
        await store.get_direct_write_policy(
            github_repository_id=repository_id,
            repository_full_name=repository_name,
        )
        enabled = await store.set_direct_write_policy(
            github_repository_id=repository_id,
            repository_full_name=repository_name,
            enabled=True,
            expected_revision=0,
            actor_id="github-user-42",
            admin_ip="203.0.113.42",
        )
        assert enabled is not None and enabled["policy"]["revision"] == 1

        claim = asyncio.create_task(
            _direct_write_token_for_claim(
                repository_name,
                store=store,
                app_credentials=credentials,
                job_id="revision-race-claim",
            )
        )
        await asyncio.wait_for(mint_started.wait(), timeout=5)
        disabled = await store.set_direct_write_policy(
            github_repository_id=repository_id,
            repository_full_name=repository_name,
            enabled=False,
            expected_revision=1,
            actor_id="github-user-42",
            admin_ip="203.0.113.42",
        )
        assert disabled is not None and disabled["policy"]["revision"] == 2
        release_mint.set()

        with pytest.raises(DirectWritePolicyUnavailable):
            await asyncio.wait_for(claim, timeout=5)
        assert credentials.revoked == ["stale-installation-token"]
        assert await store.list_direct_write_tokens(github_repository_id=repository_id) == []
    finally:
        release_mint.set()
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM agent_repository_direct_write_policy WHERE github_repository_id = $1",
                repository_id,
            )
            await conn.execute(
                "DELETE FROM agent_admin_audit_log WHERE details->>'github_repository_id' = $1",
                str(repository_id),
            )


async def test_cancel_after_direct_write_token_registration_revokes_and_forgets_token(
    store: PostgresSourceControlRepository,
    pool,
) -> None:
    """Cancellation after durable registration revokes and removes the undisclosed token."""
    repository_id = uuid.uuid4().int & ((1 << 62) - 1)
    repository_name = "octocat/cancelled-claim-project"

    class CancellingStore:
        async def get_direct_write_policy(self, **kwargs: Any) -> dict[str, Any]:
            return await store.get_direct_write_policy(**kwargs)

        async def register_direct_write_token(self, **kwargs: Any) -> bool:
            registered = await store.register_direct_write_token(**kwargs)
            assert registered
            raise asyncio.CancelledError

        async def forget_direct_write_token(self, **kwargs: Any) -> None:
            await store.forget_direct_write_token(**kwargs)

    class Credentials:
        def __init__(self) -> None:
            self.direct_write_cipher = SourceControlCipher("test-direct-write-cancel-key")
            self.revoked: list[str] = []

        async def repository_identity(self, repo: str) -> dict[str, Any]:
            assert repo == repository_name
            return {"repository_id": repository_id, "full_name": repository_name}

        async def token_for(self, repo: str, **kwargs: Any) -> str:
            assert repo == repository_name
            assert kwargs["cache"] is False
            return "cancelled-installation-token"

        async def revoke_installation_token(self, token: str) -> None:
            self.revoked.append(token)

    credentials = Credentials()
    try:
        await store.get_direct_write_policy(
            github_repository_id=repository_id,
            repository_full_name=repository_name,
        )
        enabled = await store.set_direct_write_policy(
            github_repository_id=repository_id,
            repository_full_name=repository_name,
            enabled=True,
            expected_revision=0,
            actor_id="github-user-42",
            admin_ip="203.0.113.42",
        )
        assert enabled is not None

        with pytest.raises(asyncio.CancelledError):
            await _direct_write_token_for_claim(
                repository_name,
                store=CancellingStore(),  # type: ignore[arg-type]
                app_credentials=credentials,
                job_id="cancelled-registration-claim",
            )

        assert credentials.revoked == ["cancelled-installation-token"]
        assert await store.list_direct_write_tokens(github_repository_id=repository_id) == []
    finally:
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM agent_repository_direct_write_policy WHERE github_repository_id = $1",
                repository_id,
            )
            await conn.execute(
                "DELETE FROM agent_admin_audit_log WHERE details->>'github_repository_id' = $1",
                str(repository_id),
            )
