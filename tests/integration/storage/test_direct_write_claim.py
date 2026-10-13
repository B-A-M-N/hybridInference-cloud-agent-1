"""Postgres-backed verification of direct-write claim credential lifecycle."""

from __future__ import annotations

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


async def test_direct_write_token_cache_is_reused_only_for_the_same_policy_revision(
    store: PostgresSourceControlRepository,
    pool,
) -> None:
    """Postgres tracks one cached token per live revision, including reenablement."""
    repository_id = uuid.uuid4().int & ((1 << 62) - 1)
    repository_name = "octocat/cached-project"

    class CachedCredentials:
        def __init__(self) -> None:
            self.direct_write_cipher = SourceControlCipher("test-direct-write-cache-key")
            self.cache: dict[str, str] = {}
            self.cache_tags: list[str] = []

        async def repository_identity(self, repo: str) -> dict[str, Any]:
            assert repo == repository_name
            return {"repository_id": repository_id, "full_name": repository_name}

        async def token_for(self, repo: str, **kwargs: Any) -> str:
            assert repo == repository_name
            tag = str(kwargs["cache_tag"])
            self.cache_tags.append(tag)
            if tag not in self.cache:
                self.cache[tag] = f"installation-token-{len(self.cache) + 1}"
            return self.cache[tag]

        async def revoke_installation_token(self, token: str) -> None:
            return None

    credentials = CachedCredentials()
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

        first = await _direct_write_token_for_claim(
            repository_name,
            store=store,
            app_credentials=credentials,
            job_id="cache-first",
        )
        reused = await _direct_write_token_for_claim(
            repository_name,
            store=store,
            app_credentials=credentials,
            job_id="cache-follow-up",
        )
        assert first == reused == ("installation-token-1", repository_name)
        assert credentials.cache_tags == [
            "direct-write-policy:1",
            "direct-write-policy:1",
        ]
        assert len(await store.list_direct_write_tokens(github_repository_id=repository_id)) == 1

        disabled = await store.set_direct_write_policy(
            github_repository_id=repository_id,
            repository_full_name=repository_name,
            enabled=False,
            expected_revision=1,
            actor_id="github-user-42",
            admin_ip="203.0.113.42",
        )
        assert disabled is not None and disabled["policy"]["revision"] == 2
        for token in disabled["tokens"]:
            plaintext = credentials.direct_write_cipher.decrypt(token["token_ciphertext"])
            await credentials.revoke_installation_token(plaintext)
            await store.forget_direct_write_token(token_hash=token["token_hash"])

        reenabled = await store.set_direct_write_policy(
            github_repository_id=repository_id,
            repository_full_name=repository_name,
            enabled=True,
            expected_revision=2,
            actor_id="github-user-42",
            admin_ip="203.0.113.42",
        )
        assert reenabled is not None and reenabled["policy"]["revision"] == 3
        rotated = await _direct_write_token_for_claim(
            repository_name,
            store=store,
            app_credentials=credentials,
            job_id="cache-reenabled",
        )
        assert rotated == ("installation-token-2", repository_name)
        assert credentials.cache_tags[-1] == "direct-write-policy:3"
        rows = await store.list_direct_write_tokens(github_repository_id=repository_id)
        assert len(rows) == 1
        assert credentials.direct_write_cipher.decrypt(rows[0]["token_ciphertext"]) == (
            "installation-token-2"
        )
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
