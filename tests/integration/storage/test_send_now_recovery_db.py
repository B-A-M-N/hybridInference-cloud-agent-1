"""Two processes can claim one send-now handoff; only one of them injects.

The lock is a session advisory lock on a connection that is not in the pool.
The database is required (``-m dbtest``); a missing one fails, never skips.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from typing import Any

import asyncpg
import pytest
import pytest_asyncio
from tests.support.db import reset_agent_tables
from tests.support.migrations import apply_migrations

from cloud_agent.api import turns
from cloud_agent.storage.assembly import Storage

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_THREAD = "athr_sendnow"
_JOB = "ajob_sendnow"
_USER = "user-1"
_MESSAGE = "apmsg_sendnow"
_LOCK_SQL = "SELECT pg_try_advisory_lock(hashtextextended('send-now:' || $1, 0))"
_UNLOCK_SQL = "SELECT pg_advisory_unlock(hashtextextended('send-now:' || $1, 0))"


def _db_config() -> dict[str, Any]:
    """The test database; anything not named ``*_test_*`` is refused."""
    name = os.getenv("TEST_DB_NAME", "issue503_test_db")
    worker = os.getenv("PYTEST_XDIST_WORKER", "master")
    if worker != "master":
        name = f"{name}_{worker}"
    if "_test_" not in name:
        pytest.fail(f"SAFETY: TEST_DB_NAME='{name}' is not a test database.")
    return {
        "host": os.getenv("TEST_DB_HOST", "127.0.0.1"),
        "port": int(os.getenv("TEST_DB_PORT", "5432")),
        "database": name,
        "user": os.getenv("TEST_DB_USER", "postgres"),
        "password": os.getenv("TEST_DB_PASSWORD", "postgres"),
    }


@pytest_asyncio.fixture
async def pool(monkeypatch):
    """The test database at head, emptied before and after the case."""
    config = _db_config()
    try:
        created = await asyncpg.create_pool(**config, min_size=1, max_size=5)
    except Exception as exc:
        pytest.fail(f"PostgreSQL is required for the dbtest tier: {exc}")
    # Migrations prefer this variable over the URL the fixture just built.
    monkeypatch.delenv("AGENT_DATABASE_URL", raising=False)
    apply_migrations(config)
    async with created.acquire() as conn:
        await reset_agent_tables(conn)
    try:
        yield created
    finally:
        async with created.acquire() as conn:
            await reset_agent_tables(conn)
        await created.close()


async def _seed(pool: asyncpg.Pool) -> int:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO agent_threads (id, user_id, repo, title, execution_profile)
            VALUES ($1, $2, 'o/n', 't', 'trusted-v1')
            """,
            _THREAD,
            _USER,
        )
        await conn.execute(
            """
            INSERT INTO agent_jobs (
                id, thread_id, user_id, turn_no, task_prompt, runtime, model, state
            )
            VALUES ($1, $2, $3, 1, 'fix it', 'claude-code', 'm', 'running')
            """,
            _JOB,
            _THREAD,
            _USER,
        )
        attempt_id = await conn.fetchval(
            """
            INSERT INTO agent_attempts (
                job_id, thread_id, attempt_no, lease_owner, lease_generation,
                lease_expires_at, status
            )
            VALUES ($1, $2, 1, 'runner', 1, NOW() + INTERVAL '5 minutes', 'running')
            RETURNING id
            """,
            _JOB,
            _THREAD,
        )
        await conn.execute(
            "UPDATE agent_jobs SET current_attempt_id = $2 WHERE id = $1",
            _JOB,
            attempt_id,
        )
        await conn.execute(
            """
            INSERT INTO agent_pending_messages (
                id, thread_id, user_id, state, revision, prompt,
                send_now_job_id, send_now_requested_at
            )
            VALUES
                ($1, $2, $3, 'sending', 2, 'and mind the tests', $4, NOW()),
                ('apmsg_queued', $2, $3, 'queued', 1, 'later', NULL, NULL),
                ('apmsg_untied', $2, $3, 'sending', 2, 'no turn', NULL, NULL)
            """,
            _MESSAGE,
            _THREAD,
            _USER,
            _JOB,
        )
    return int(attempt_id)


async def _lock_is_held(pool: asyncpg.Pool) -> bool:
    """True when this session cannot take the message's lock.

    A pooled connection that does take it unlocks before it is returned: the
    pool's reset would drop every advisory lock on that session otherwise.
    """
    async with pool.acquire() as conn:
        locked = await conn.fetchval(_LOCK_SQL, _MESSAGE)
        if locked:
            await conn.execute(_UNLOCK_SQL, _MESSAGE)
        return not locked


async def test_two_concurrent_claims_inject_once(pool, monkeypatch):
    attempt_id = await _seed(pool)
    store = Storage(pool)
    turns._completed_send_now.clear()
    listed = await store.pending_messages.list_sending_handoffs()
    assert [row["id"] for row in listed] == [_MESSAGE]
    message = listed[0]
    assert (
        await store.pending_messages.mark_send_now_handed(message_id=_MESSAGE, revision=9) is False
    )
    assert [row["id"] for row in await store.pending_messages.list_sending_handoffs()] == [_MESSAGE]

    entered = asyncio.Event()
    release = asyncio.Event()
    handed: list[int] = []

    async def hand_over(_store, *, thread_id, attempt_id, message):
        handed.append(attempt_id)
        entered.set()
        await release.wait()
        return None

    monkeypatch.setattr(turns, "_hand_over", hand_over)
    first = asyncio.create_task(
        turns.run_send_now_handoff(
            store,
            thread_id=_THREAD,
            job_id=_JOB,
            message=message,
            gateway=None,
            app_credentials=None,
        )
    )
    winner = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert await _lock_is_held(pool)
        second = await turns.run_send_now_handoff(
            store,
            thread_id=_THREAD,
            job_id=_JOB,
            message=message,
            gateway=None,
            app_credentials=None,
        )
        assert second.injected is False
        assert handed == [attempt_id]
        release.set()
        winner = await first
    finally:
        release.set()
        if not first.done():
            first.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await first
    assert winner is not None and winner.injected is True
    assert handed == [attempt_id]
    assert not await _lock_is_held(pool)
    async with pool.acquire() as conn:
        marked = await conn.fetchval(
            "SELECT send_now_handed_off FROM agent_pending_messages WHERE id = $1",
            _MESSAGE,
        )
    assert marked is True
    assert await store.pending_messages.list_sending_handoffs() == []
    # A new process has neither the lock nor the in-memory set. The mark is
    # what keeps it from injecting the text the host already accepted.
    turns._completed_send_now.clear()
    restarted = await turns.run_send_now_handoff(
        store,
        thread_id=_THREAD,
        job_id=_JOB,
        message=message,
        gateway=None,
        app_credentials=None,
    )
    assert restarted.injected is False
    assert handed == [attempt_id]
    assert not await _lock_is_held(pool)

    third = await turns.run_send_now_handoff(
        store,
        thread_id=_THREAD,
        job_id=_JOB,
        message=message,
        gateway=None,
        app_credentials=None,
    )
    assert third.injected is False
    assert handed == [attempt_id]

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_pending_messages SET state = 'queued' WHERE id = $1",
            _MESSAGE,
        )
    turns._completed_send_now.discard((_MESSAGE, int(message["revision"])))
    left = await turns.run_send_now_handoff(
        store,
        thread_id=_THREAD,
        job_id=_JOB,
        message=message,
        gateway=None,
        app_credentials=None,
    )
    assert left.injected is False
    assert handed == [attempt_id]
    assert not await _lock_is_held(pool)
    turns._completed_send_now.clear()
