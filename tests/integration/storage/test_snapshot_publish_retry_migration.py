"""The durable publication retry counter is a linear, reversible revision."""

from __future__ import annotations

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from tests.integration.storage.test_agent_job_store import pool as pool
from tests.integration.storage.test_schema_upgrade import _db_config
from tests.support.migrations import REPO, apply_migrations, downgrade_to

pytestmark = pytest.mark.dbtest

_BEFORE = "0050_gitlab_refresh_lease"
_THIS = "0051_snapshot_publish_retries"


def test_snapshot_publish_retry_revision_follows_gitlab_refresh_lease():
    """The counter follows 0050 directly without introducing a branch."""
    config = Config(str(REPO / "alembic.ini"))
    config.set_main_option("script_location", str(REPO / "migrations"))
    script = ScriptDirectory.from_config(config)

    assert script.get_revision(_THIS).down_revision == _BEFORE


@pytest.mark.asyncio
async def test_snapshot_publish_retry_counter_downgrades_and_upgrades_cleanly(pool):
    """Rollback removes the counter; re-upgrade restores its durable guardrails."""
    config = _db_config()
    try:
        downgrade_to(config, _BEFORE)
        async with pool.acquire() as conn:
            missing = await conn.fetchval(
                """
                SELECT COUNT(*) FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'agent_sandbox_snapshots'
                  AND column_name = 'publish_retry_count'
                """
            )
            constraint = await conn.fetchval(
                """
                SELECT COUNT(*) FROM pg_constraint
                WHERE conname = 'ck_agent_sandbox_snapshots_publish_retry_count'
                """
            )
        assert missing == 0
        assert constraint == 0

        apply_migrations(config, _THIS)
        async with pool.acquire() as conn:
            column = await conn.fetchrow(
                """
                SELECT is_nullable, column_default
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'agent_sandbox_snapshots'
                  AND column_name = 'publish_retry_count'
                """
            )
            definition = await conn.fetchval(
                """
                SELECT pg_get_constraintdef(oid)
                FROM pg_constraint
                WHERE conname = 'ck_agent_sandbox_snapshots_publish_retry_count'
                """
            )
        assert dict(column) == {"is_nullable": "NO", "column_default": "0"}
        assert "publish_retry_count >= 0" in definition
    finally:
        apply_migrations(config)
