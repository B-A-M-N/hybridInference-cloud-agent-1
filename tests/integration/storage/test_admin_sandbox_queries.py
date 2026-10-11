"""Real Postgres checks for running retained groups and current occupancy."""

import json
from datetime import timedelta
from uuid import UUID

import asyncpg
import pytest
from tests.integration.storage.test_agent_job_store import (
    _claim,
    _create_job,
    _reserve_shared_chat,
    _shared_claim_attestation,
)
from tests.integration.storage.test_attempt_ledger import _seed_attempt, db as db, store as store
from tests.unit.test_ledger_snapshots import END, START
from tests.unit.test_sandbox_resource_protocol import sample

from cloud_agent.admin_observability import decode_cursor
from cloud_agent.admin_sandboxes import (
    SANDBOX_CURSOR_SCOPE,
    SANDBOX_TRAJECTORY_MEMBER_LIMIT,
    SandboxCursor,
    SandboxTrajectoryCursor,
    sandbox_trajectory_cursor_scope,
)
from cloud_agent.goals.models import GoalInput
from cloud_agent.storage.admin_sandbox_queries import AdminSandboxQueries

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

TEST_HOST_CAPACITY_EPOCH = UUID("00000000-0000-0000-0000-000000000001")
REPLACEMENT_HOST_CAPACITY_EPOCH = UUID("00000000-0000-0000-0000-000000000002")


async def seed(
    db,
    store,
    sandbox_id,
    *,
    state="running",
    host="runner-one",
    register_host=True,
    **job_options,
):
    """Create a real origin thread and a retained runtime without claiming work."""
    job = await _create_job(
        store,
        task_prompt="PRIVATE task prompt",
        metadata={"credential": "PRIVATE token"},
        **job_options,
    )
    async with db.acquire() as conn:
        if register_host:
            await conn.execute(
                """INSERT INTO agent_runner_hosts (host, credential_hash, capacity_epoch)
                   VALUES ($1, 'test-credential-hash', $2)
                   ON CONFLICT (host) DO UPDATE
                   SET credential_hash = EXCLUDED.credential_hash,
                       capacity_epoch = EXCLUDED.capacity_epoch""",
                host,
                TEST_HOST_CAPACITY_EPOCH,
            )
        await conn.execute(
            """INSERT INTO agent_sandboxes
               (id, user_id, origin_thread_id, host, container_id, container_name,
                runtime_id, runtime_version, execution_profile, memory_limit, cpu_limit,
                state, created_at, last_activity_at, idle_deadline, updated_at, last_error,
                host_capacity_epoch)
               VALUES ($1, $2, $3, $4, $5, $6, 'claude-code', '2.1.220',
                       'untrusted-kata-v1', '8g', '4', $7, $8, $9, $10, $9, 'PRIVATE error',
                       $11)""",
            sandbox_id,
            job["user_id"],
            job["thread_id"],
            host,
            f"container-{sandbox_id}",
            f"agent-{sandbox_id}",
            state,
            START,
            END,
            END + timedelta(minutes=30),
            TEST_HOST_CAPACITY_EPOCH if register_host else None,
        )
    return job


async def member(db, sandbox_id, job, slot, *, attempt=None, fork_from=None):
    """A reserved slot can be starting; a historical member can have no slot."""
    async with db.acquire() as conn:
        await conn.execute(
            """INSERT INTO agent_thread_sandbox_memberships
               (thread_id, user_id, sandbox_id, resident_slot_id, active_attempt_id,
                resident_state, native_session_id, forked_from_thread_id,
                fork_anchor_job_id, last_error)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'PRIVATE member error')""",
            job["thread_id"],
            job["user_id"],
            sandbox_id,
            slot,
            attempt,
            "starting" if slot is not None else "stopped",
            f"PRIVATE-session-{job['thread_id']}",
            fork_from["thread_id"] if fork_from else None,
            fork_from["id"] if fork_from else None,
        )


async def listing(db, *, cursor=None, limit=100):
    return await AdminSandboxQueries(db).list_running(cursor=cursor, limit=limit)


async def stop_request(db, store, sandbox_id, **overrides):
    """Stop precisely the metadata an administrator has just reviewed."""
    detail = await store.admin_sandboxes.detail(sandbox_id)
    assert detail is not None
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandboxes SET terminal_count = 0, terminal_count_observed_at = NOW() "
            "WHERE id = $1 AND terminal_count IS NULL",
            sandbox_id,
        )
    return await store.sandbox.request_admin_stop(
        **(
            {
                "sandbox_id": sandbox_id,
                "generation": detail.sandbox.generation,
                "container_id": detail.sandbox.container_id,
                "last_activity_at": detail.sandbox.last_activity_at,
                "actor_id": "admin:operator",
                "admin_ip": "127.0.0.1",
            }
            | overrides
        )
    )


async def delete_request(db, store, sandbox_id, **overrides):
    """Delete precisely the stopped metadata an administrator reviewed."""
    detail = await store.admin_sandboxes.detail(sandbox_id)
    assert detail is not None and detail.sandbox.container_id is not None
    return await store.sandbox.request_admin_delete(
        **(
            {
                "sandbox_id": sandbox_id,
                "generation": detail.sandbox.generation,
                "container_id": detail.sandbox.container_id,
                "last_activity_at": detail.sandbox.last_activity_at,
                "actor_id": "admin:operator",
                "admin_ip": "127.0.0.1",
            }
            | overrides
        )
    )


async def test_all_states_and_detail_include_safe_current_members(db, store):
    origin = await seed(db, store, "asbx_stopped", state="stopped")
    await seed(db, store, "asbx_lost", state="lost")
    await member(db, "asbx_stopped", origin, 0)
    page = await store.admin_sandboxes.list_sandboxes(cursor=None, limit=1, state="all")
    assert page.total_count == 2 and page.next_cursor is not None
    only_stopped = await store.admin_sandboxes.list_sandboxes(
        cursor=None, limit=100, state="stopped"
    )
    assert [row.id for row in only_stopped.sandboxes] == ["asbx_stopped"]
    detail = await store.admin_sandboxes.detail("asbx_stopped")
    assert detail is not None and detail.sandbox.state == "stopped"
    assert detail.members[0].thread_id == origin["thread_id"]
    assert "PRIVATE" not in detail.model_dump_json()
    assert await store.admin_sandboxes.detail("missing") is None


async def test_trajectory_pages_newest_first_with_all_members_and_deleted_tombstone(db, store):
    origin = await seed(db, store, "asbx_trajectory")
    historical = await _create_job(store, user_id=origin["user_id"], repo=origin["repo"])
    await member(db, "asbx_trajectory", origin, 0)
    await member(db, "asbx_trajectory", historical, None)
    async with db.acquire() as conn:
        await conn.execute("UPDATE agent_sandboxes SET state='stopping' WHERE id='asbx_trajectory'")
        await conn.execute("UPDATE agent_sandboxes SET state='stopped' WHERE id='asbx_trajectory'")

    first = await store.admin_sandboxes.trajectory("asbx_trajectory", cursor=None, limit=2)
    assert first is not None and len(first.events) == 2
    assert [event.id for event in first.events] == sorted(
        [event.id for event in first.events], reverse=True
    )
    assert first.next_cursor is not None
    assert first.member_history_count == 2
    assert first.member_history_truncated is False
    assert first.members[0].thread_id == origin["thread_id"]
    assert {item.thread_id for item in first.members} == {
        origin["thread_id"],
        historical["thread_id"],
    }
    assert any(item.resident_slot_id is None for item in first.members)
    cursor = decode_cursor(
        first.next_cursor,
        SandboxTrajectoryCursor,
        sandbox_trajectory_cursor_scope("asbx_trajectory"),
    )
    assert cursor is not None

    # A later event cannot leak into continuation pages from the fixed snapshot.
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandboxes SET snapshot_state='pending' WHERE id='asbx_trajectory'"
        )
    second = await store.admin_sandboxes.trajectory("asbx_trajectory", cursor=cursor, limit=100)
    assert second is not None
    assert all(event.id <= cursor.snapshot_id for event in second.events)
    assert not ({event.id for event in first.events} & {event.id for event in second.events})

    async with db.acquire() as conn:
        await conn.execute(
            """
            UPDATE agent_sandboxes
            SET snapshot_state='quiescent', delete_requested_at=NOW(),
                delete_requested_by='admin:operator',
                delete_requested_host_capacity_epoch=host_capacity_epoch, deleted_at=NOW()
            WHERE id='asbx_trajectory'
            """
        )
    inventory = await store.admin_sandboxes.list_sandboxes(cursor=None, limit=100, state="all")
    assert "asbx_trajectory" not in {sandbox.id for sandbox in inventory.sandboxes}
    tombstone = await store.admin_sandboxes.trajectory("asbx_trajectory", cursor=None, limit=100)
    assert tombstone is not None and tombstone.sandbox.deleted_at is not None
    assert {item.thread_id for item in tombstone.members} == {
        origin["thread_id"],
        historical["thread_id"],
    }
    assert "PRIVATE" not in tombstone.model_dump_json()


async def test_trajectory_bounds_member_history_and_keeps_origin_first(db, store):
    origin = await seed(db, store, "asbx_many_members")
    await member(db, "asbx_many_members", origin, 0)
    historical_thread_ids = [
        f"athr_historical_{index:03d}" for index in range(SANDBOX_TRAJECTORY_MEMBER_LIMIT)
    ]
    async with db.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO agent_threads (id, user_id, repo, title)
            VALUES ($1, $2, $3, $4)
            """,
            [
                (thread_id, origin["user_id"], origin["repo"], f"Historical {index}")
                for index, thread_id in enumerate(historical_thread_ids)
            ],
        )
        await conn.executemany(
            """
            INSERT INTO agent_thread_sandbox_memberships
                (thread_id, user_id, sandbox_id, created_at, updated_at)
            VALUES ($1, $2, 'asbx_many_members', $3, $3)
            """,
            [(thread_id, origin["user_id"], START) for thread_id in historical_thread_ids],
        )

    trajectory = await store.admin_sandboxes.trajectory("asbx_many_members", cursor=None, limit=100)

    assert trajectory is not None
    assert trajectory.member_history_count == SANDBOX_TRAJECTORY_MEMBER_LIMIT + 1
    assert trajectory.member_history_truncated is True
    assert len(trajectory.members) == SANDBOX_TRAJECTORY_MEMBER_LIMIT
    assert trajectory.members[0].thread_id == origin["thread_id"]


async def test_admin_delete_request_is_fenced_idempotent_audited_and_closes_access(db, store):
    origin = await seed(db, store, "asbx_delete", state="stopped")
    await member(db, "asbx_delete", origin, 0)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_thread_sandbox_memberships SET resident_state='stopped' "
            "WHERE sandbox_id='asbx_delete'"
        )
    assert await store.sandbox.thread_workspace_ready(thread_id=origin["thread_id"])

    assert await delete_request(db, store, "asbx_delete") == "accepted"
    assert await delete_request(db, store, "asbx_delete") == "accepted"
    assert not await store.sandbox.thread_workspace_ready(thread_id=origin["thread_id"])

    detail = await store.admin_sandboxes.detail("asbx_delete")
    assert detail is not None
    assert detail.sandbox.delete_requested_at is not None
    assert detail.sandbox.delete_requested_by == "admin:operator"
    assert detail.sandbox.deleted_at is None
    async with db.acquire() as conn:
        audits = await conn.fetch(
            "SELECT * FROM agent_admin_audit_log WHERE action='agent.sandbox.delete'"
        )
        events = await conn.fetch(
            "SELECT event_type, source, actor_id, details "
            "FROM agent_sandbox_events WHERE sandbox_id='asbx_delete' "
            "AND event_type='admin_delete_requested'"
        )
    assert len(audits) == 1
    assert audits[0]["actor_id"] == "admin:operator"
    assert audits[0]["target_user_id"] == origin["user_id"]
    assert len(events) == 1
    assert events[0]["source"] == "admin"
    assert events[0]["actor_id"] == "admin:operator"
    assert events[0]["details"] == {
        "host": "runner-one",
        "host_capacity_epoch": str(TEST_HOST_CAPACITY_EPOCH),
    }
    assert "PRIVATE" not in json.dumps(dict(events[0]), default=str)


async def test_host_delete_completion_tombstones_and_cleans_exact_stopped_group(db, store):
    origin = await seed(db, store, "asbx_delete_complete", state="stopped")
    await member(db, "asbx_delete_complete", origin, 0)
    async with db.acquire() as conn:
        await conn.execute(
            """
            UPDATE agent_thread_sandbox_memberships
            SET resident_state='stopped', residency_generation=9,
                admitted_job_id=$2, admitted_at=NOW(), last_settled_job_id=$2
            WHERE sandbox_id=$1
            """,
            "asbx_delete_complete",
            origin["id"],
        )
        await conn.execute(
            "UPDATE agent_jobs SET state='succeeded', native_turn_no=turn_no, "
            "terminal_resume_pending=TRUE WHERE id=$1",
            origin["id"],
        )
        await conn.execute(
            """
            UPDATE agent_sandboxes
            SET resource_snapshot=$2::jsonb, resource_sampled_at=NOW(),
                resource_received_at=NOW(), terminal_count=0,
                terminal_count_observed_at=NOW()
            WHERE id=$1
            """,
            "asbx_delete_complete",
            json.dumps(sample()),
        )
    follow_up = await store.conversation_commands.create_follow_up(
        parent_job_id=origin["id"],
        user_id=origin["user_id"],
        prompt="This queued turn must not survive sandbox deletion",
    )
    assert follow_up is not None
    goal = await store.goals.set_goal(
        thread_id=origin["thread_id"],
        user_id=origin["user_id"],
        goal=GoalInput(objective="keep working until this is done"),
    )
    assert goal is not None and goal["state"] == "active"
    assert await delete_request(db, store, "asbx_delete_complete") == "accepted"

    due = await store.sandbox.due_admin_sandbox_deletions(
        host="runner-one",
        authenticated_host_capacity_epoch=TEST_HOST_CAPACITY_EPOCH,
        limit=10,
    )
    assert len(due) == 1
    command = due[0]
    assert command["sandbox_id"] == "asbx_delete_complete"
    assert command["thread_id"] == origin["thread_id"]
    assert (
        await store.sandbox.due_admin_sandbox_deletions(
            host="runner-two",
            authenticated_host_capacity_epoch=TEST_HOST_CAPACITY_EPOCH,
            limit=10,
        )
        == []
    )
    assert (
        await store.sandbox.confirm_admin_sandbox_deleted(
            sandbox_id=command["sandbox_id"],
            generation=command["generation"],
            container_id=command["container_id"],
            host="runner-two",
            host_capacity_epoch=command["host_capacity_epoch"],
            authenticated_host_capacity_epoch=TEST_HOST_CAPACITY_EPOCH,
            outcome="deleted",
        )
        == "stale"
    )

    assert (
        await store.sandbox.confirm_admin_sandbox_deleted(
            sandbox_id=command["sandbox_id"],
            generation=command["generation"],
            container_id=command["container_id"],
            host="runner-one",
            host_capacity_epoch=command["host_capacity_epoch"],
            authenticated_host_capacity_epoch=TEST_HOST_CAPACITY_EPOCH,
            outcome="deleted",
        )
        == "confirmed"
    )
    # A lost HTTP response retries with the original physical identity after
    # container_id has been cleared; the durable tombstone is the receipt.
    assert (
        await store.sandbox.confirm_admin_sandbox_deleted(
            sandbox_id=command["sandbox_id"],
            generation=command["generation"],
            container_id=command["container_id"],
            host="runner-one",
            host_capacity_epoch=command["host_capacity_epoch"],
            authenticated_host_capacity_epoch=TEST_HOST_CAPACITY_EPOCH,
            outcome="already_absent",
        )
        == "already_confirmed"
    )
    assert (
        await store.sandbox.due_admin_sandbox_deletions(
            host="runner-one",
            authenticated_host_capacity_epoch=TEST_HOST_CAPACITY_EPOCH,
            limit=10,
        )
        == []
    )

    # A heartbeat sampled before deletion can arrive after the durable receipt.
    # It must observe, but never repopulate, the physical tombstone.
    assert (
        await store.sandbox.reconcile_thread_sandbox_observation(
            thread_id=origin["thread_id"],
            host="runner-one",
            authenticated_host_capacity_epoch=TEST_HOST_CAPACITY_EPOCH,
            sandbox_id="asbx_delete_complete",
            container_id=command["container_id"],
            sandbox_generation=command["generation"],
            running=True,
            activity_active=True,
            members=[
                {
                    "thread_id": origin["thread_id"],
                    "activation_generation": 0,
                    "process_generation": 99,
                    "residency_generation": 9,
                    "resident_state": "live",
                }
            ],
            resource_usage=sample(),
            terminal_count=3,
        )
        == "stopped"
    )

    async with db.acquire() as conn:
        group = await conn.fetchrow(
            """
            SELECT state, container_id, delete_requested_at, delete_requested_by,
                   deleted_at, resource_snapshot, terminal_count
            FROM agent_sandboxes WHERE id='asbx_delete_complete'
            """
        )
        membership = await conn.fetchrow(
            """
            SELECT resident_state, context_status, native_state, resident_slot_id,
                   residency_generation, residency_status, residency_grant_id,
                   admitted_job_id, admitted_at, last_settled_job_id
            FROM agent_thread_sandbox_memberships
            WHERE sandbox_id='asbx_delete_complete'
            """
        )
        deleted_events = await conn.fetch(
            """
            SELECT from_state, to_state, source, actor_id, details
            FROM agent_sandbox_events
            WHERE sandbox_id='asbx_delete_complete' AND event_type='deleted'
            """
        )
        audits = await conn.fetch(
            "SELECT action FROM agent_admin_audit_log WHERE action='agent.sandbox.delete'"
        )
    assert group["state"] == "stopped"
    assert group["container_id"] is None
    assert group["delete_requested_at"] is not None
    assert group["delete_requested_by"] == "admin:operator"
    assert group["deleted_at"] is not None
    assert group["resource_snapshot"] is None
    assert group["terminal_count"] is None
    assert membership["resident_state"] == "lost"
    assert membership["context_status"] == "lost"
    assert membership["native_state"] == "lost"
    assert membership["resident_slot_id"] is None
    assert membership["residency_generation"] == 9
    assert membership["residency_status"] == "none"
    assert membership["residency_grant_id"] is None
    assert membership["admitted_job_id"] == origin["id"]
    assert membership["admitted_at"] is not None
    assert membership["last_settled_job_id"] == origin["id"]
    assert len(deleted_events) == 1
    assert dict(deleted_events[0]) == {
        "from_state": "stopped",
        "to_state": "deleted",
        "source": "host",
        "actor_id": None,
        "details": {
            "host": "runner-one",
            "host_capacity_epoch": str(TEST_HOST_CAPACITY_EPOCH),
            "outcome": "deleted",
        },
    }
    assert len(audits) == 1
    deleted_follow_up = await store.conversation_queries.get_job(follow_up["id"])
    assert deleted_follow_up is not None
    assert deleted_follow_up["state"] == "failed"
    deleted_origin = await store.conversation_queries.get_job(origin["id"])
    assert deleted_origin is not None
    assert deleted_origin["terminal_resume_pending"] is False
    paused_goal = await store.goals.get_goal(thread_id=origin["thread_id"])
    assert paused_goal is not None
    assert (paused_goal["state"], paused_goal["pause_reason"]) == (
        "paused",
        "context_lost",
    )
    assert paused_goal["pause_detail"] == (
        "an administrator deleted the retained sandbox; the conversation cannot continue"
    )


async def test_legacy_group_binds_only_from_current_exact_host_observation(db, store):
    origin = await seed(db, store, "asbx_legacy_epoch", state="stopped")
    await member(db, "asbx_legacy_epoch", origin, 0)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandboxes SET host_capacity_epoch=NULL WHERE id='asbx_legacy_epoch'"
        )
        await conn.execute(
            "UPDATE agent_thread_sandbox_memberships SET resident_state='stopped' "
            "WHERE sandbox_id='asbx_legacy_epoch'"
        )

    assert await delete_request(db, store, "asbx_legacy_epoch") == "host_incarnation_unverified"
    assert (
        await store.sandbox.reconcile_thread_sandbox_observation(
            **observation(origin, sandbox_id="asbx_legacy_epoch", running=False)
        )
        == "stopped"
    )
    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT host_capacity_epoch FROM agent_sandboxes WHERE id='asbx_legacy_epoch'"
            )
            == TEST_HOST_CAPACITY_EPOCH
        )
    assert await delete_request(db, store, "asbx_legacy_epoch") == "accepted"


async def test_replacement_host_cannot_rebind_a_stamped_group(db, store):
    origin = await seed(db, store, "asbx_stamped_epoch")
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_runner_hosts SET capacity_epoch=$1 WHERE host='runner-one'",
            REPLACEMENT_HOST_CAPACITY_EPOCH,
        )

    assert (
        await store.sandbox.reconcile_thread_sandbox_observation(
            **observation(
                origin,
                sandbox_id="asbx_stamped_epoch",
                authenticated_host_capacity_epoch=REPLACEMENT_HOST_CAPACITY_EPOCH,
            )
        )
        is None
    )
    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT host_capacity_epoch FROM agent_sandboxes WHERE id='asbx_stamped_epoch'"
            )
            == TEST_HOST_CAPACITY_EPOCH
        )


async def test_reenrolled_host_cannot_receive_or_confirm_old_delete(db, store):
    origin = await seed(db, store, "asbx_reenrolled_delete", state="stopped")
    await member(db, "asbx_reenrolled_delete", origin, 0)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_thread_sandbox_memberships SET resident_state='stopped' "
            "WHERE sandbox_id='asbx_reenrolled_delete'"
        )
    assert await delete_request(db, store, "asbx_reenrolled_delete") == "accepted"
    due = await store.sandbox.due_admin_sandbox_deletions(
        host="runner-one",
        authenticated_host_capacity_epoch=TEST_HOST_CAPACITY_EPOCH,
        limit=10,
    )
    assert len(due) == 1
    command = due[0]

    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_runner_hosts SET capacity_epoch=$1 WHERE host='runner-one'",
            REPLACEMENT_HOST_CAPACITY_EPOCH,
        )
    assert (
        await store.sandbox.due_admin_sandbox_deletions(
            host="runner-one",
            authenticated_host_capacity_epoch=REPLACEMENT_HOST_CAPACITY_EPOCH,
            limit=10,
        )
        == []
    )
    assert (
        await store.sandbox.confirm_admin_sandbox_deleted(
            sandbox_id=command["sandbox_id"],
            generation=command["generation"],
            container_id=command["container_id"],
            host="runner-one",
            host_capacity_epoch=command["host_capacity_epoch"],
            authenticated_host_capacity_epoch=REPLACEMENT_HOST_CAPACITY_EPOCH,
            outcome="already_absent",
        )
        == "stale"
    )
    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT deleted_at FROM agent_sandboxes WHERE id='asbx_reenrolled_delete'"
            )
            is None
        )


@pytest.mark.parametrize(
    "fence", [{"generation": 2}, {"container_id": "replacement"}, {"last_activity_at": START}]
)
async def test_delete_rejects_a_stale_review(db, store, fence):
    await seed(db, store, "asbx_delete_stale", state="stopped")
    assert await delete_request(db, store, "asbx_delete_stale", **fence) == "stale"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("state='running'", "not_stopped"),
        ("active_operation_count=1", "busy"),
        ("snapshot_state='pending'", "busy"),
    ],
)
async def test_delete_refuses_running_or_nonquiescent_groups(db, store, change, reason):
    await seed(db, store, "asbx_delete_refuse", state="stopped")
    async with db.acquire() as conn:
        await conn.execute(f"UPDATE agent_sandboxes SET {change} WHERE id='asbx_delete_refuse'")
    assert await delete_request(db, store, "asbx_delete_refuse") == reason
    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT COUNT(*) FROM agent_admin_audit_log WHERE action='agent.sandbox.delete'"
            )
            == 0
        )


async def test_delete_refuses_a_hot_member_residency(db, store):
    origin = await seed(db, store, "asbx_delete_hot", state="stopped")
    await member(db, "asbx_delete_hot", origin, 0)
    async with db.acquire() as conn:
        await conn.execute(
            """
            UPDATE agent_thread_sandbox_memberships
            SET resident_state='stopped', residency_status='hot',
                residency_lease_expires_at=NOW() + INTERVAL '5 minutes'
            WHERE sandbox_id='asbx_delete_hot'
            """
        )
    assert await delete_request(db, store, "asbx_delete_hot") == "busy"


async def test_delete_audit_failure_rolls_back_intent_and_event(db, store):
    await seed(db, store, "asbx_delete_audit", state="stopped")
    async with db.acquire() as conn:
        await conn.execute(
            "ALTER TABLE agent_admin_audit_log ADD CONSTRAINT reject_delete_test "
            "CHECK (action <> 'agent.sandbox.delete')"
        )
    try:
        with pytest.raises(asyncpg.CheckViolationError):
            await delete_request(db, store, "asbx_delete_audit")
        detail = await store.admin_sandboxes.detail("asbx_delete_audit")
        assert detail is not None and detail.sandbox.delete_requested_at is None
        async with db.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM agent_sandbox_events "
                    "WHERE sandbox_id='asbx_delete_audit' "
                    "AND event_type='admin_delete_requested'"
                )
                == 0
            )
    finally:
        async with db.acquire() as conn:
            await conn.execute(
                "ALTER TABLE agent_admin_audit_log DROP CONSTRAINT reject_delete_test"
            )


async def test_admin_stop_survives_running_heartbeats_and_clears_on_confirmation(db, store):
    origin = await seed(db, store, "asbx_stop")
    await member(db, "asbx_stop", origin, 0)
    assert await stop_request(db, store, "asbx_stop") == "accepted"
    assert await stop_request(db, store, "asbx_stop") == "accepted"
    assert not await store.sandbox.thread_workspace_ready(thread_id=origin["thread_id"])
    observed = {
        "thread_id": origin["thread_id"],
        "host": "runner-one",
        "sandbox_id": "asbx_stop",
        "container_id": "container-asbx_stop",
        "sandbox_generation": 1,
    }
    for _ in range(2):
        assert (
            await store.sandbox.reconcile_thread_sandbox_observation(
                **observed,
                authenticated_host_capacity_epoch=TEST_HOST_CAPACITY_EPOCH,
                running=True,
                activity_active=False,
                idle_remaining_seconds=1800,
                terminal_count=0,
            )
            == "stopping"
        )
        command = await store.sandbox.due_thread_sandbox_stop(**observed)
        assert command is not None and command["reason"] == "idle"
        assert command["member_activation_generations"] == {origin["thread_id"]: 0}
    detail = await store.admin_sandboxes.detail("asbx_stop")
    assert detail is not None and detail.sandbox.stop_requested_at is not None
    assert (
        await store.sandbox.reconcile_thread_sandbox_observation(
            **observed,
            authenticated_host_capacity_epoch=TEST_HOST_CAPACITY_EPOCH,
            running=False,
            terminal_count=0,
        )
        == "stopped"
    )
    detail = await store.admin_sandboxes.detail("asbx_stop")
    assert detail is not None and detail.sandbox.stop_requested_at is None
    assert await store.sandbox.thread_workspace_ready(thread_id=origin["thread_id"])
    async with db.acquire() as conn:
        logs = await conn.fetch(
            "SELECT * FROM agent_admin_audit_log WHERE action = 'agent.sandbox.stop'"
        )
    assert len(logs) == 1
    assert logs[0]["actor_id"] == "admin:operator"
    assert logs[0]["target_user_id"] == origin["user_id"]


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("active_operation_count = 1", "busy"),
        ("snapshot_state = 'pending'", "busy"),
        ("snapshot_state = 'snapshotting'", "busy"),
        ("snapshot_state = 'importing'", "busy"),
        ("terminal_count = 1", "terminals_open"),
        ("terminal_count_observed_at = NOW() - INTERVAL '2 minutes'", "activity_unknown"),
        ("state = 'stopped'", "not_running"),
    ],
)
async def test_stop_refuses_busy_or_unobservable_groups(db, store, change, reason):
    await seed(db, store, "asbx_refuse")
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandboxes SET terminal_count=0, terminal_count_observed_at=NOW()"
        )
        await conn.execute(f"UPDATE agent_sandboxes SET {change} WHERE id='asbx_refuse'")
    assert await stop_request(db, store, "asbx_refuse") == reason
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT COUNT(*) FROM agent_admin_audit_log") == 0


@pytest.mark.parametrize(
    "fence", [{"generation": 2}, {"container_id": "replacement"}, {"last_activity_at": START}]
)
async def test_stop_rejects_stale_review(db, store, fence):
    await seed(db, store, "asbx_stale")
    assert await stop_request(db, store, "asbx_stale", **fence) == "stale"


async def test_stop_audit_failure_rolls_back_intent(db, store):
    await seed(db, store, "asbx_audit")
    async with db.acquire() as conn:
        await conn.execute(
            "ALTER TABLE agent_admin_audit_log ADD CONSTRAINT reject_stop_test "
            "CHECK (action <> 'agent.sandbox.stop')"
        )
    try:
        with pytest.raises(asyncpg.CheckViolationError):
            await stop_request(db, store, "asbx_audit")
        detail = await store.admin_sandboxes.detail("asbx_audit")
        assert detail is not None and detail.sandbox.state == "running"
        assert detail.sandbox.stop_requested_at is None
    finally:
        async with db.acquire() as conn:
            await conn.execute("ALTER TABLE agent_admin_audit_log DROP CONSTRAINT reject_stop_test")


async def test_stop_fences_claims_until_confirmation_then_allows_native_resume(db, store):
    job, claim, sandbox = await _reserve_shared_chat(store)
    fence = {
        "thread_id": job["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    assert await store.sandbox.mark_thread_sandbox_running(
        **fence, container_id="container-controlled", context_status="new"
    )
    assert await stop_request(db, store, sandbox["sandbox_id"]) == "busy"
    assert await store.sandbox.admit_thread_sandbox_turn(**fence, job_id=job["id"])
    assert await store.sandbox.settle_thread_sandbox(
        **fence,
        job_id=job["id"],
        context_status="live",
        native_session_id="native-control",
        idle_timeout_seconds=1800,
        base_sha=job["base_sha"],
    )
    assert await store.attempt_commands.transition(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )
    # This test starts after the completed publication pipeline. Unfinished
    # snapshots are a separate stop blocker and must not wake the group later.
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandbox_snapshots SET state='failed' WHERE sandbox_id=$1",
            sandbox["sandbox_id"],
        )
        await conn.execute(
            "UPDATE agent_sandboxes SET snapshot_state='quiescent' WHERE id=$1",
            sandbox["sandbox_id"],
        )
    follow_up = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"],
        user_id=job["user_id"],
        prompt="Resume after the administrator stop",
    )
    assert follow_up is not None
    assert await stop_request(db, store, sandbox["sandbox_id"]) == "accepted"
    claim_options = {
        "worker_id": "admin-control",
        "lease_ttl_seconds": 60,
        "host": "shared-runner",
        "host_credential_hash": "test-credential-hash",
        "claim_attestation": _shared_claim_attestation(),
    }
    assert await _claim(store, **claim_options) is None
    assert (
        await store.sandbox.reconcile_thread_sandbox_observation(
            thread_id=job["thread_id"],
            host="shared-runner",
            authenticated_host_capacity_epoch=sandbox["host_capacity_epoch"],
            sandbox_id=sandbox["sandbox_id"],
            container_id="container-controlled",
            sandbox_generation=sandbox["generation"],
            running=False,
            terminal_count=0,
        )
        == "stopped"
    )
    resumed = await _claim(store, **claim_options)
    assert resumed is not None and resumed["id"] == follow_up["id"]
    # Claim has committed before reservation: the membership still has no
    # active_attempt_id, but it is already too late to accept a stop.
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandboxes SET state='running' WHERE id=$1", sandbox["sandbox_id"]
        )
    assert await stop_request(db, store, sandbox["sandbox_id"]) == "busy"
    reserve = {
        key: sandbox[key]
        for key in (
            "sandbox_id",
            "container_name",
            "runtime_id",
            "runtime_version",
            "sandbox_image_digest",
            "execution_profile",
            "host",
        )
    }
    reserved = await store.sandbox.reserve_thread_sandbox(
        **reserve,
        thread_id=job["thread_id"],
        attempt_id=resumed["attempt_id"],
        lease_generation=resumed["lease_generation"],
    )
    assert (
        reserved is not None
        and reserved["activation_generation"] > sandbox["activation_generation"]
    )


async def test_running_idle_groups_include_all_owners_and_missing_hosts(db, store):
    idle = await seed(
        db,
        store,
        "asbx_idle",
        host="forgotten-host",
        register_host=False,
        user_id="owner-one",
    )
    busy = await seed(
        db,
        store,
        "asbx_busy",
        host="known-host",
        register_host=False,
        user_id="owner-two",
    )
    _, attempt, _ = await _seed_attempt(db, store, job=busy, grant_id=None)
    await member(db, "asbx_idle", idle, 0)
    await member(db, "asbx_busy", busy, 0, attempt=attempt)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET state = 'succeeded', native_turn_no = turn_no WHERE id = $1",
            idle["id"],
        )
        await conn.execute(
            "UPDATE agent_sandboxes SET container_id = NULL, runtime_version = NULL WHERE id = 'asbx_idle'"
        )
        await conn.execute(
            """INSERT INTO agent_runner_hosts
               (host, first_seen_at, last_seen_at, last_heartbeat_at, status)
               VALUES ('known-host', $1, $2, $2, 'offline')""",
            START,
            END,
        )
    page = await store.admin_sandboxes.list_running(cursor=None, limit=100)
    assert page.total_count == 2
    assert {row.user_id for row in page.sandboxes} == {"owner-one", "owner-two"}
    by_id = {row.id: row for row in page.sandboxes}
    assert by_id["asbx_idle"].active_attempt_count == 0
    assert by_id["asbx_idle"].container_id is None
    assert by_id["asbx_idle"].runtime_version is None
    assert by_id["asbx_idle"].host_last_heartbeat_at is None
    assert by_id["asbx_busy"].active_attempt_count == 1
    assert by_id["asbx_busy"].host_last_heartbeat_at == END
    assert by_id["asbx_busy"].created_at == START
    assert by_id["asbx_busy"].last_activity_at == by_id["asbx_busy"].updated_at == END
    assert by_id["asbx_busy"].idle_deadline == END + timedelta(minutes=30)
    assert by_id["asbx_busy"].repo == busy["repo"]
    assert by_id["asbx_busy"].memory_limit == "8g"
    assert by_id["asbx_busy"].cpu_limit == "4"
    assert page.observed_at.tzinfo is not None
    assert "PRIVATE" not in page.model_dump_json()


async def test_only_running_sandbox_state_is_included(db, store):
    await seed(db, store, "asbx_running")
    for state in ["provisioning", "starting", "stopping", "stopped", "stopped_dirty", "lost"]:
        await seed(db, store, f"asbx_{state}", state=state)
    page = await listing(db)
    assert [row.id for row in page.sandboxes] == ["asbx_running"]
    assert page.total_count == 1


@pytest.mark.parametrize("container_state", ["running", "stopped"])
async def test_follow_up_reservation_keeps_running_container_visible(db, store, container_state):
    """Member startup preserves a live group without claiming a stopped one is running."""
    job, claim, sandbox = await _reserve_shared_chat(store)
    fence = {
        "thread_id": job["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    assert await store.sandbox.mark_thread_sandbox_running(
        **fence, container_id="container-retained-inventory", context_status="new"
    )
    assert await store.sandbox.admit_thread_sandbox_turn(**fence, job_id=job["id"])
    settled = await store.sandbox.settle_thread_sandbox(
        **fence,
        job_id=job["id"],
        context_status="live",
        native_session_id="retained-inventory-session",
        idle_timeout_seconds=1800,
        base_sha=job["base_sha"],
    )
    assert settled is not None and settled["idle_deadline"] is not None
    assert await store.attempt_commands.transition(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )
    if container_state == "stopped":
        async with db.acquire() as conn:
            await conn.execute(
                "UPDATE agent_sandboxes SET state = 'stopped', idle_deadline = NULL WHERE id = $1",
                sandbox["sandbox_id"],
            )
    follow_up = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"], user_id=job["user_id"], prompt="Continue in the retained container"
    )
    assert follow_up is not None and follow_up.get("error") is None
    activated = await _claim(
        store,
        worker_id="inventory-follow-up",
        lease_ttl_seconds=60,
        host="shared-runner",
        host_credential_hash="test-credential-hash",
        claim_attestation=_shared_claim_attestation(),
    )
    assert activated is not None and activated["id"] == follow_up["id"]
    reserve = {
        "thread_id": job["thread_id"],
        "attempt_id": activated["attempt_id"],
        "lease_generation": activated["lease_generation"],
        "host": sandbox["host"],
        "sandbox_id": sandbox["sandbox_id"],
        "container_name": sandbox["container_name"],
        "runtime_id": sandbox["runtime_id"],
        "runtime_version": sandbox["runtime_version"],
        "sandbox_image_digest": sandbox["sandbox_image_digest"],
        "execution_profile": sandbox["execution_profile"],
    }
    reserved = await store.sandbox.reserve_thread_sandbox(**reserve)
    assert reserved is not None
    assert reserved["state"] == ("running" if container_state == "running" else "starting")
    assert reserved["container_id"] == "container-retained-inventory"
    assert reserved["idle_deadline"] is None
    assert reserved["active_attempt_id"] == activated["attempt_id"]
    assert reserved["activation_generation"] > sandbox["activation_generation"]
    repeated = await store.sandbox.reserve_thread_sandbox(**reserve)
    assert repeated is not None
    assert repeated["activation_generation"] == reserved["activation_generation"]
    page = await store.admin_sandboxes.list_running(cursor=None, limit=100)
    assert page.total_count == (1 if container_state == "running" else 0)
    if container_state == "running":
        assert [row.id for row in page.sandboxes] == [sandbox["sandbox_id"]]
        assert page.sandboxes[0].active_attempt_count == 1
    else:
        assert page.sandboxes == []


async def test_forks_share_one_row_and_counts_use_current_reserved_slots(db, store):
    origin = await seed(db, store, "asbx_group")
    fork = await _create_job(store)
    historical = await _create_job(store)
    _, origin_attempt, _ = await _seed_attempt(db, store, job=origin, grant_id=None)
    _, fork_attempt, _ = await _seed_attempt(db, store, job=fork, grant_id=None)
    await member(db, "asbx_group", origin, 0, attempt=origin_attempt)
    await member(db, "asbx_group", fork, 1, attempt=fork_attempt, fork_from=origin)
    await member(db, "asbx_group", historical, None, fork_from=origin)
    async with db.acquire() as conn:
        await conn.execute(
            """UPDATE agent_thread_sandbox_memberships
               SET native_state = 'lost', resident_state = 'lost' WHERE thread_id = $1""",
            historical["thread_id"],
        )
    page = await listing(db)
    assert len(page.sandboxes) == page.total_count == 1
    row = page.sandboxes[0]
    assert row.origin_thread_id == origin["thread_id"]
    assert row.member_count == 2  # Reserved slots count even while a member starts.
    assert row.active_attempt_count == 2
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_thread_sandbox_memberships SET active_attempt_id = NULL WHERE thread_id = $1",
            fork["thread_id"],
        )
    row = (await listing(db)).sandboxes[0]
    assert row.member_count == 2
    assert row.active_attempt_count == 1


async def test_id_pagination_is_complete_without_duplicate_groups(db, store):
    for sandbox_id in ["asbx_c", "asbx_a", "asbx_b"]:
        await seed(db, store, sandbox_id)
    await seed(db, store, "asbx_stopped", state="stopped")
    seen = []
    cursor = None
    while True:
        page = await listing(db, cursor=cursor, limit=1)
        assert page.total_count == 3
        assert len(page.sandboxes) == 1
        seen.extend(row.id for row in page.sandboxes)
        if page.next_cursor is None:
            break
        cursor = decode_cursor(page.next_cursor, SandboxCursor, SANDBOX_CURSOR_SCOPE)
        assert cursor.after_id == seen[-1]
    assert seen == ["asbx_a", "asbx_b", "asbx_c"]
    exhausted = await listing(
        db, cursor=SandboxCursor(scope=SANDBOX_CURSOR_SCOPE, after_id="asbx_z")
    )
    assert exhausted.sandboxes == []
    assert exhausted.total_count == 3
    assert exhausted.next_cursor is None


async def test_empty_inventory_is_a_successful_observation(db, store):
    page = await listing(db)
    assert page.sandboxes == []
    assert page.total_count == 0
    assert page.next_cursor is None
    assert page.observed_at.tzinfo is not None


@pytest.mark.parametrize("source", ["generated", "manual", "fallback", "inherited"])
async def test_titles_require_safe_provenance_and_are_bounded(db, store, source):
    job = await seed(db, store, "asbx_title")
    title = "Approved title " + "x" * 500 if source in ["generated", "manual"] else "PRIVATE title"
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_threads SET title = $2, title_source = $3 WHERE id = $1",
            job["thread_id"],
            title,
            source,
        )
    page = await listing(db)
    assert page.sandboxes[0].title == (title[:160] if source in ["generated", "manual"] else None)
    assert "PRIVATE" not in page.model_dump_json()


def observation(job, sandbox_id="asbx_resources", **overrides):
    """An authenticated Docker observation names the exact durable group."""
    return {
        "thread_id": job["thread_id"],
        "host": "runner-one",
        "authenticated_host_capacity_epoch": TEST_HOST_CAPACITY_EPOCH,
        "sandbox_id": sandbox_id,
        "container_id": f"container-{sandbox_id}",
        "sandbox_generation": 1,
        "running": True,
        **overrides,
    }


async def stored_resources(db, sandbox_id="asbx_resources"):
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT resource_snapshot, resource_sampled_at, resource_received_at,
                      terminal_count, terminal_count_observed_at
               FROM agent_sandboxes WHERE id = $1""",
            sandbox_id,
        )
    result = dict(row)
    if isinstance(result["resource_snapshot"], str):
        result["resource_snapshot"] = json.loads(result["resource_snapshot"])
    return result


@pytest.mark.parametrize("count", [0, 4096])
async def test_direct_terminal_observation_accepts_observed_count_bounds(db, store, count):
    await seed(db, store, "asbx_resources")
    assert await store.sandbox.record_terminal_observation(
        sandbox_id="asbx_resources",
        host="runner-one",
        generation=1,
        container_id="container-asbx_resources",
        terminal_count=count,
    )
    row = await stored_resources(db)
    assert row["terminal_count"] == count
    assert row["terminal_count_observed_at"] is not None
    assert row["resource_snapshot"] is None


@pytest.mark.parametrize("count", [None, True, -1, 4097, 1.0, "1"])
async def test_direct_terminal_observation_rejects_invalid_counts_without_writes(db, store, count):
    await seed(db, store, "asbx_resources")
    previous = await stored_resources(db)
    with pytest.raises(ValueError, match="Invalid terminal count"):
        await store.sandbox.record_terminal_observation(
            sandbox_id="asbx_resources",
            host="runner-one",
            generation=1,
            container_id="container-asbx_resources",
            terminal_count=count,
        )
    assert await stored_resources(db) == previous


@pytest.mark.parametrize(
    ("bad_fence", "state"),
    [
        ({"sandbox_id": "asbx_missing"}, "running"),
        ({"host": "different-host"}, "running"),
        ({"generation": 2}, "running"),
        ({"container_id": "different-container"}, "running"),
        ({}, "stopping"),
        ({}, "stopped"),
        ({}, "stopped_dirty"),
        ({}, "lost"),
        ({}, "provisioning"),
    ],
)
async def test_direct_terminal_observation_requires_exact_running_container(
    db, store, bad_fence, state
):
    job = await seed(db, store, "asbx_resources")
    await store.sandbox.reconcile_thread_sandbox_observation(**observation(job), terminal_count=2)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandboxes SET state = $1 WHERE id = 'asbx_resources'", state
        )
    previous = await stored_resources(db)
    assert not await store.sandbox.record_terminal_observation(
        **(
            {
                "sandbox_id": "asbx_resources",
                "host": "runner-one",
                "generation": 1,
                "container_id": "container-asbx_resources",
                "terminal_count": 0,
            }
            | bad_fence
        )
    )
    assert await stored_resources(db) == previous


async def test_container_resources_include_source_age_and_current_group_stats(db, store):
    job = await seed(db, store, "asbx_resources")
    await member(db, "asbx_resources", job, 0)
    measured = sample(sample_age_ms=12345.0)
    async with db.acquire() as conn:
        await conn.execute(
            """UPDATE agent_sandboxes
               SET resident_slot_capacity = 16, active_operation_count = 2,
                   snapshot_state = 'snapshotting'
               WHERE id = 'asbx_resources'"""
        )
    assert (
        await store.sandbox.reconcile_thread_sandbox_observation(
            **observation(job), resource_usage=measured, terminal_count=3
        )
        == "running"
    )
    raw = await stored_resources(db)
    assert raw["resource_snapshot"] == measured
    assert raw["resource_received_at"] - raw["resource_sampled_at"] == timedelta(milliseconds=12345)
    page = await listing(db)
    row = page.sandboxes[0]
    usage = row.resource_usage
    assert usage is not None
    assert usage.sample_id == measured["sample_id"]
    assert usage.source == "docker-stats"
    assert usage.scope == "container"
    assert usage.sampled_at == raw["resource_sampled_at"]
    assert usage.received_at == raw["resource_received_at"]
    assert usage.age_seconds == pytest.approx(
        (page.observed_at - raw["resource_sampled_at"]).total_seconds()
    )
    assert usage.stale is False
    assert usage.cpu_cores == 1.25
    assert usage.cpu_window_seconds == 2.0
    assert usage.memory_bytes == 512 * 1024**2
    assert usage.memory_limit_bytes == 4 * 1024**3
    assert usage.pids == 12
    assert usage.io_read_bytes_per_second == 10.0
    assert usage.net_tx_bytes == 4000
    assert row.member_count == 1
    assert row.resident_slot_capacity == 16
    assert row.active_operation_count == 2
    assert row.snapshot_state == "snapshotting"
    assert row.terminal_count == 3
    assert row.terminal_count_observed_at == raw["terminal_count_observed_at"]
    assert "sample_age_ms" not in usage.model_dump()
    assert "resource_snapshot" not in page.model_dump_json()
    assert "PRIVATE" not in page.model_dump_json()


async def test_duplicate_and_older_samples_cannot_freshen_or_replace_resources(db, store):
    job = await seed(db, store, "asbx_resources")
    measured = sample(sample_age_ms=10000.0)
    await store.sandbox.reconcile_thread_sandbox_observation(
        **observation(job), resource_usage=measured
    )
    first = await stored_resources(db)
    await store.sandbox.reconcile_thread_sandbox_observation(
        **observation(job), resource_usage=sample(sample_age_ms=0.0, memory_bytes=1)
    )
    assert await stored_resources(db) == first
    await store.sandbox.reconcile_thread_sandbox_observation(
        **observation(job),
        resource_usage=sample(sample_id="older-sample", sample_age_ms=100000.0, memory_bytes=2),
    )
    assert await stored_resources(db) == first
    newer = sample(sample_id="newer-sample", sample_age_ms=0.0, memory_bytes=3)
    await store.sandbox.reconcile_thread_sandbox_observation(
        **observation(job), resource_usage=newer
    )
    replaced = await stored_resources(db)
    assert replaced["resource_snapshot"] == newer
    assert replaced["resource_sampled_at"] > first["resource_sampled_at"]
    assert replaced["resource_received_at"] > first["resource_received_at"]


@pytest.mark.parametrize(
    "bad_fence",
    [
        {"host": "different-host"},
        {"thread_id": "athr_wrong-origin"},
        {"sandbox_id": "asbx_missing"},
        {"sandbox_generation": 2},
        {"container_id": "different-container"},
    ],
)
async def test_resource_writes_obey_every_sandbox_identity_fence(db, store, bad_fence):
    job = await seed(db, store, "asbx_resources")
    await store.sandbox.reconcile_thread_sandbox_observation(
        **observation(job), resource_usage=sample(), terminal_count=1
    )
    previous = await stored_resources(db)
    assert (
        await store.sandbox.reconcile_thread_sandbox_observation(
            **observation(job, **bad_fence),
            resource_usage=sample(sample_id="forged-sample", memory_bytes=999),
            terminal_count=99,
        )
        is None
    )
    assert await stored_resources(db) == previous


@pytest.mark.parametrize("invalid", [None, {}, {"source": "host"}, sample(memory_bytes=-1)])
async def test_missing_or_bad_resource_samples_preserve_age_and_clear_unknown_terminal_count(
    db, store, invalid
):
    job = await seed(db, store, "asbx_resources")
    await store.sandbox.reconcile_thread_sandbox_observation(
        **observation(job), resource_usage=sample(sample_age_ms=120000.0), terminal_count=2
    )
    previous = await stored_resources(db)
    assert (
        await store.sandbox.reconcile_thread_sandbox_observation(
            **observation(job), resource_usage=invalid
        )
        == "running"
    )
    current = await stored_resources(db)
    for field in ("resource_snapshot", "resource_sampled_at", "resource_received_at"):
        assert current[field] == previous[field]
    assert current["terminal_count"] is None
    assert current["terminal_count_observed_at"] is None
    row = (await listing(db)).sandboxes[0]
    assert row.resource_usage is not None
    assert row.resource_usage.stale is True
    assert row.resource_usage.age_seconds >= 120
    assert row.terminal_count is None


async def test_stopped_observation_clears_previous_container_measurements(db, store):
    job = await seed(db, store, "asbx_resources")
    await store.sandbox.reconcile_thread_sandbox_observation(
        **observation(job), resource_usage=sample(), terminal_count=2
    )
    assert (
        await store.sandbox.reconcile_thread_sandbox_observation(
            **observation(job, running=False),
            resource_usage=sample(sample_id="late-stopped-sample"),
            terminal_count=2,
        )
        == "stopped"
    )
    row = await stored_resources(db)
    assert row["resource_snapshot"] is None
    assert row["resource_sampled_at"] is None
    assert row["resource_received_at"] is None
    assert row["terminal_count"] is None
    assert row["terminal_count_observed_at"] is None
    assert (await listing(db)).sandboxes == []
    await store.sandbox.reconcile_thread_sandbox_observation(**observation(job))
    assert (await listing(db)).sandboxes[0].resource_usage is None


@pytest.mark.parametrize(
    "corrupt",
    [
        {"credential": "PRIVATE token"},
        sample(source="host-memory"),
        sample(pids=-1),
        sample(task_prompt="PRIVATE prompt"),
    ],
)
async def test_corrupt_persisted_resources_do_not_break_or_leak_through_inventory(
    db, store, corrupt
):
    await seed(db, store, "asbx_resources")
    await seed(db, store, "asbx_other")
    async with db.acquire() as conn:
        await conn.execute(
            """UPDATE agent_sandboxes SET resource_snapshot = $1::jsonb,
                      resource_sampled_at = NOW(), resource_received_at = NOW()
               WHERE id = 'asbx_resources'""",
            json.dumps(corrupt),
        )
    page = await listing(db)
    assert page.total_count == 2
    assert len(page.sandboxes) == 2
    assert all(row.resource_usage is None for row in page.sandboxes)
    assert "PRIVATE" not in page.model_dump_json()


async def test_database_rejects_non_object_resource_snapshots(db, store):
    await seed(db, store, "asbx_resources")
    async with db.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE agent_sandboxes SET resource_snapshot = $1::jsonb WHERE id = $2",
                json.dumps("PRIVATE malformed snapshot"),
                "asbx_resources",
            )
    assert (await listing(db)).sandboxes[0].resource_usage is None


async def test_valid_measurement_repairs_corrupt_snapshot_without_sample_identity(db, store):
    job = await seed(db, store, "asbx_resources")
    async with db.acquire() as conn:
        await conn.execute(
            """UPDATE agent_sandboxes SET resource_snapshot = $1::jsonb,
                      resource_sampled_at = NOW() - INTERVAL '1 minute',
                      resource_received_at = NOW() - INTERVAL '1 minute'
               WHERE id = 'asbx_resources'""",
            json.dumps({"credential": "PRIVATE token"}),
        )
    assert (await listing(db)).sandboxes[0].resource_usage is None
    await store.sandbox.reconcile_thread_sandbox_observation(
        **observation(job), resource_usage=sample(sample_age_ms=0.0)
    )
    row = (await listing(db)).sandboxes[0]
    assert row.resource_usage is not None
    assert row.resource_usage.sample_id == "resource-sample-1"
    assert (await stored_resources(db))["resource_snapshot"] == sample(sample_age_ms=0.0)
    assert "PRIVATE" not in row.model_dump_json()


@pytest.mark.parametrize(
    ("method", "invalid_update"),
    [
        ("_record_terminal_observation", "terminal_count = -1"),
        ("_record_resource_observation", "resource_snapshot = '[]'::jsonb"),
    ],
)
async def test_observation_database_failures_do_not_roll_back_container_lifecycle(
    db, store, monkeypatch, method, invalid_update
):
    job = await seed(db, store, "asbx_resources")

    async def unavailable_telemetry(conn, *, sandbox_id, **_kwargs):
        await conn.execute(
            f"UPDATE agent_sandboxes SET {invalid_update} WHERE id = $1",
            sandbox_id,
        )

    monkeypatch.setattr(store.sandbox, method, unavailable_telemetry)
    assert (
        await store.sandbox.reconcile_thread_sandbox_observation(
            **observation(job, running=False), resource_usage=sample()
        )
        == "stopped"
    )
    async with db.acquire() as conn:
        assert (
            await conn.fetchval("SELECT state FROM agent_sandboxes WHERE id = 'asbx_resources'")
            == "stopped"
        )
    assert (await stored_resources(db))["terminal_count"] is None


@pytest.mark.parametrize(
    ("previous_count", "observed_count", "stop_result"),
    [(None, 0, "accepted"), (0, 1, "terminals_open")],
)
async def test_resource_sql_failure_preserves_fresh_terminal_stop_safety(
    db, store, previous_count, observed_count, stop_result
):
    job = await seed(db, store, "asbx_resources")
    await store.sandbox.reconcile_thread_sandbox_observation(
        **observation(job), resource_usage=sample(), terminal_count=previous_count
    )
    previous = await stored_resources(db)
    detail = await store.admin_sandboxes.detail("asbx_resources")
    assert detail is not None
    request = {
        "sandbox_id": detail.sandbox.id,
        "generation": detail.sandbox.generation,
        "container_id": detail.sandbox.container_id,
        "last_activity_at": detail.sandbox.last_activity_at,
        "actor_id": "admin:operator",
        "admin_ip": "127.0.0.1",
    }
    if previous_count is None:
        assert await store.sandbox.request_admin_stop(**request) == "activity_unknown"
    async with db.acquire() as conn:
        await conn.execute(
            "ALTER TABLE agent_sandboxes ADD CONSTRAINT test_reject_resource_sample "
            "CHECK (resource_snapshot->>'sample_id' IS DISTINCT FROM 'unavailable-sample')"
        )
    try:
        assert (
            await store.sandbox.reconcile_thread_sandbox_observation(
                **observation(job),
                resource_usage=sample(sample_id="unavailable-sample", sample_age_ms=0.0),
                terminal_count=observed_count,
            )
            == "running"
        )
        current = await stored_resources(db)
        for field in ("resource_snapshot", "resource_sampled_at", "resource_received_at"):
            assert current[field] == previous[field]
        assert current["terminal_count"] == observed_count
        assert current["terminal_count_observed_at"] is not None
        if previous["terminal_count_observed_at"] is not None:
            assert current["terminal_count_observed_at"] > previous["terminal_count_observed_at"]
        assert await store.sandbox.request_admin_stop(**request) == stop_result
    finally:
        async with db.acquire() as conn:
            await conn.execute(
                "ALTER TABLE agent_sandboxes DROP CONSTRAINT test_reject_resource_sample"
            )


async def test_nullable_resource_measurements_remain_unknown_in_admin_inventory(db, store):
    job = await seed(db, store, "asbx_resources")
    await store.sandbox.reconcile_thread_sandbox_observation(
        **observation(job),
        resource_usage=sample(
            cpu_cores=None, memory_limit_bytes=None, net_rx_bytes_per_second=None
        ),
        terminal_count=0,
    )
    row = (await listing(db)).sandboxes[0]
    assert row.terminal_count == 0
    assert row.resource_usage is not None
    assert row.resource_usage.cpu_cores is None
    assert row.resource_usage.memory_limit_bytes is None
    assert row.resource_usage.net_rx_bytes_per_second is None
