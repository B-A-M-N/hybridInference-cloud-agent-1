"""A new chat in an existing workspace, on real Postgres.

``create_workspace_chat`` makes a conversation that is a member of another
chat's sandbox group from the start: one container and ``/workspace``, a
resident slot of its own, and no native session until its first turn opens
one. Only a replica that attests ``workspace-chat-v1`` may claim that first
turn — an earlier runner would prepare the workspace as new and reset it — and
the workspace takes the chat only once the chat that created it has settled.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.integration.storage.test_agent_job_store import (
    _SHARED_HOST_CAPABILITIES,
    _claim,
    _create_job,
    _reserve_shared_chat,
    _shared_claim_attestation,
    pool as pool,
    store as store,
)

from cloud_agent.domain.ssh_servers import SshServerUnavailable
from cloud_agent_shared.shared_protocol import WORKSPACE_CHAT_CAPABILITY

if TYPE_CHECKING:
    from cloud_agent.storage.assembly import Storage

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_HOST = "shared-runner"
_CONTAINER = "container-shared-one"
_JOINING_CAPABILITIES = [*_SHARED_HOST_CAPABILITIES, WORKSPACE_CHAT_CAPABILITY]


def _joining_attestation() -> dict[str, object]:
    """A replica of this release: it can start a chat in an existing workspace."""
    return {**_shared_claim_attestation(), "runner_capabilities": _JOINING_CAPABILITIES}


async def _claim_here(
    store: Storage, worker_id: str, attestation: dict[str, object] | None = None
) -> dict | None:
    """Claim on the workspace's host, as an earlier release or as this one."""
    return await _claim(
        store,
        worker_id=worker_id,
        lease_ttl_seconds=60,
        host=_HOST,
        host_credential_hash="test-credential-hash",
        claim_attestation=attestation or _shared_claim_attestation(),
    )


async def _host_can_join(store: Storage, *, capable: bool = True) -> None:
    await store.host_pool.record_host_heartbeat(
        host=_HOST,
        capabilities=_JOINING_CAPABILITIES if capable else _SHARED_HOST_CAPABILITIES,
        sandbox_image_digest="sha256:shared-image",
    )


async def _settle(store: Storage, job: dict, claim: dict, sandbox: dict, native: str) -> None:
    """Run one turn to its settled native boundary, as the runner reports it."""
    fence = {
        "thread_id": job["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    assert await store.sandbox.mark_thread_sandbox_running(
        **fence, container_id=_CONTAINER, context_status="new"
    )
    assert await store.sandbox.admit_thread_sandbox_turn(
        **fence, residency_generation=sandbox["residency_generation"], job_id=job["id"]
    )
    assert await store.sandbox.settle_thread_sandbox(
        **fence,
        residency_generation=sandbox["residency_generation"],
        job_id=job["id"],
        context_status="live",
        native_session_id=native,
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


async def _settled_workspace(store: Storage, **reserve: object) -> tuple[dict, dict]:
    """A chat whose first turn prepared its workspace and settled."""
    job, claim, sandbox = await _reserve_shared_chat(store, **reserve)  # type: ignore[arg-type]
    await _settle(store, job, claim, sandbox, "native-origin")
    await _host_can_join(store)
    return job, sandbox


async def _join(store: Storage, source: dict, **overrides: object) -> dict | None:
    params: dict[str, object] = {
        "source_job_id": source["id"],
        "user_id": source["user_id"],
        "task_prompt": "check the README",
        "model": "glm-5.1",
    }
    params.update(overrides)
    return await store.conversation_commands.create_workspace_chat(**params)  # type: ignore[arg-type]


async def _reserve(store: Storage, job: dict, claim: dict, sandbox: dict) -> dict:
    """Reserve the claimed chat's member in the group it was created into."""
    reserved = await store.sandbox.reserve_thread_sandbox(
        thread_id=job["thread_id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        host=_HOST,
        sandbox_id=sandbox["sandbox_id"],
        container_name=sandbox["container_name"],
        runtime_id=sandbox["runtime_id"],
        runtime_version=sandbox["runtime_version"],
        sandbox_image_digest=sandbox["sandbox_image_digest"],
        execution_profile=sandbox["execution_profile"],
    )
    assert reserved is not None
    residency = await store.residency.establish_residency(
        thread_id=job["thread_id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        host=_HOST,
        sandbox_id=sandbox["sandbox_id"],
        runtime_id=sandbox["runtime_id"],
        runtime_version=sandbox["runtime_version"],
        ttl_seconds=1800,
    )
    assert residency is not None
    return {**reserved, "residency_generation": residency["generation"]}


async def test_a_workspace_chat_joins_the_group_and_opens_its_own_session(pool, store):
    origin, sandbox = await _settled_workspace(store)
    assert (await store.sandbox.thread_sandbox(thread_id=origin["thread_id"]))[
        "workspace_chat_capable"
    ] is True

    joined = await _join(store, origin)

    assert joined is not None and joined.get("error") is None
    assert joined["thread_id"] != origin["thread_id"]
    assert joined["turn_no"] == 1
    # The workspace's facts are the source's; the conversation's are the request's.
    assert joined["repo"] == origin["repo"]
    assert joined["base_sha"] == origin["base_sha"]
    assert joined["runtime"] == sandbox["runtime_id"]
    assert joined["task_prompt"] == "check the README"
    async with pool.acquire() as conn:
        member = await conn.fetchrow(
            "SELECT sandbox_id, resident_slot_id, native_session_id, last_settled_job_id, "
            "forked_from_thread_id FROM agent_thread_sandbox_memberships WHERE thread_id = $1",
            joined["thread_id"],
        )
        history = await conn.fetchval(
            "SELECT count(*) FROM agent_jobs WHERE thread_id = $1", joined["thread_id"]
        )
    assert member["sandbox_id"] == sandbox["sandbox_id"]
    assert member["resident_slot_id"] == 1
    # Nothing of the source's history: no session, no settled turn, no lineage.
    assert member["native_session_id"] is None
    assert member["last_settled_job_id"] is None
    assert member["forked_from_thread_id"] is None
    assert history == 1
    shared = await store.sandbox.thread_sandbox(thread_id=joined["thread_id"])
    assert shared is not None
    assert shared["origin_thread_id"] == origin["thread_id"]
    assert shared["container_id"] == _CONTAINER

    # A replica of an earlier release would prepare the workspace as new.
    assert await _claim_here(store, "old") is None
    claim = await _claim_here(store, "new", _joining_attestation())
    assert claim is not None and claim["id"] == joined["id"]
    assert claim["thread_sandbox"]["sandbox_id"] == sandbox["sandbox_id"]
    reserved = await _reserve(store, joined, claim, shared)
    assert reserved["context_status"] == "new"
    assert reserved["origin_thread_id"] == origin["thread_id"]
    assert {member["thread_id"] for member in reserved["group_members"]} == {
        origin["thread_id"],
        joined["thread_id"],
    }

    await _settle(store, joined, claim, reserved, "native-joined")
    async with pool.acquire() as conn:
        sessions = dict(
            await conn.fetch(
                "SELECT thread_id, native_session_id FROM agent_thread_sandbox_memberships "
                "WHERE sandbox_id = $1",
                sandbox["sandbox_id"],
            )
        )
    assert sessions == {
        origin["thread_id"]: "native-origin",
        joined["thread_id"]: "native-joined",
    }

    # Settled, it is an ordinary member: any compatible replica continues it.
    follow_up = await store.conversation_commands.create_follow_up(
        parent_job_id=joined["id"], user_id=joined["user_id"], prompt="and the tests"
    )
    assert follow_up is not None and follow_up.get("error") is None
    resumed = await _claim_here(store, "old")
    assert resumed is not None and resumed["id"] == follow_up["id"]


async def test_a_chat_whose_first_turn_has_no_sandbox_yet_is_not_ready(store):
    """Queued, or claimed before its reservation: the workspace is on its way."""
    queued = await _create_job(store)
    assert await _join(store, queued) == {"error": "workspace_not_ready"}


async def test_a_workspace_takes_another_chat_only_after_its_origin_settles(store):
    """While the first turn prepares the workspace, a retry of it prepares it again."""
    job, claim, sandbox = await _reserve_shared_chat(store)
    await _host_can_join(store)
    assert await store.sandbox.mark_thread_sandbox_running(
        thread_id=job["thread_id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        sandbox_generation=sandbox["generation"],
        activation_generation=sandbox["activation_generation"],
        container_id=_CONTAINER,
        context_status="new",
    )

    shared = await store.sandbox.thread_sandbox(thread_id=job["thread_id"])
    assert shared is not None and shared["workspace_chat_capable"] is False
    assert await _join(store, job) == {"error": "workspace_not_ready"}


async def test_a_host_that_cannot_start_the_chat_is_not_offered_it(store):
    origin, _ = await _settled_workspace(store)
    await _host_can_join(store, capable=False)

    shared = await store.sandbox.thread_sandbox(thread_id=origin["thread_id"])
    assert shared is not None and shared["workspace_chat_capable"] is False
    assert await _join(store, origin) == {"error": "workspace_chat_unavailable"}


async def test_an_archived_or_lost_workspace_takes_no_chat(pool, store):
    origin, sandbox = await _settled_workspace(store)
    await store.conversation_commands.set_thread_archived(
        job_id=origin["id"], user_id=origin["user_id"], archived=True
    )
    assert await _join(store, origin) == {"error": "session_archived"}
    await store.conversation_commands.set_thread_archived(
        job_id=origin["id"], user_id=origin["user_id"], archived=False
    )

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandboxes SET state = 'lost' WHERE id = $1", sandbox["sandbox_id"]
        )
    shared = await store.sandbox.thread_sandbox(thread_id=origin["thread_id"])
    assert shared is not None and shared["workspace_chat_capable"] is False
    assert await _join(store, origin) == {"error": "workspace_unavailable"}
    # Another owner is told the chat does not exist.
    assert await _join(store, origin, user_id="someone-else") is None


async def test_every_resident_slot_is_one_chat_and_a_deleted_chat_gives_its_back(pool, store):
    origin, _ = await _settled_workspace(store, resident_slot_capacity=2)

    first = await _join(store, origin)
    assert first is not None and first.get("error") is None
    assert await _join(store, origin, task_prompt="another") == {
        "error": "resident_capacity_exhausted"
    }

    # The joined chat never ran, so its process is gone and no grant speaks for it.
    deleted = await store.conversation_commands.delete_sessions(
        thread_ids=[first["thread_id"]], user_id=first["user_id"]
    )
    assert deleted.get("error") is None, deleted
    second = await _join(store, origin, task_prompt="another")
    assert second is not None and second.get("error") is None
    async with pool.acquire() as conn:
        slots = dict(
            await conn.fetch(
                "SELECT thread_id, resident_slot_id FROM agent_thread_sandbox_memberships "
                "WHERE thread_id = ANY($1::text[])",
                [first["thread_id"], second["thread_id"]],
            )
        )
    assert slots == {first["thread_id"]: None, second["thread_id"]: 1}


async def test_a_refused_ssh_server_creates_nothing_and_keeps_the_slot(pool, store):
    origin, sandbox = await _settled_workspace(store, resident_slot_capacity=2)

    with pytest.raises(SshServerUnavailable):
        await _join(store, origin, ssh_server_ids=["0b9c7a4e-5d1f-4c8a-9e2b-3f6d8a1c2e4f"])

    async with pool.acquire() as conn:
        members = await conn.fetchval(
            "SELECT count(*) FROM agent_thread_sandbox_memberships WHERE sandbox_id = $1",
            sandbox["sandbox_id"],
        )
        threads = await conn.fetchval("SELECT count(*) FROM agent_threads")
    assert (members, threads) == (1, 1)
    # The slot the refused chat would have taken is still free.
    joined = await _join(store, origin)
    assert joined is not None and joined.get("error") is None


async def test_a_resent_workspace_chat_is_the_one_the_first_send_created(store):
    origin, _ = await _settled_workspace(store)

    first = await _join(
        store, origin, client_request_id="send-1", client_request_fingerprint="print-1"
    )
    again = await _join(
        store, origin, client_request_id="send-1", client_request_fingerprint="print-1"
    )
    other = await _join(
        store,
        origin,
        task_prompt="something else",
        client_request_id="send-1",
        client_request_fingerprint="print-2",
    )

    assert first is not None and again is not None
    assert again["id"] == first["id"]
    assert other == {"error": "client_request_conflict"}


async def test_a_new_task_is_unchanged_by_the_shared_insert(pool, store):
    """The insert both doors use still writes a whole new conversation."""
    job = await _create_job(store, task_prompt="a task of its own")

    assert job["turn_no"] == 1
    assert job["title_source"] == "fallback"
    async with pool.acquire() as conn:
        message = await conn.fetchval(
            "SELECT content FROM agent_thread_messages WHERE job_id = $1", job["id"]
        )
        membership = await conn.fetchval(
            "SELECT count(*) FROM agent_thread_sandbox_memberships WHERE thread_id = $1",
            job["thread_id"],
        )
    assert message == "a task of its own"
    assert membership == 0
