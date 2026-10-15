"""SQL projections for the retained sandbox read shape.

Lifted verbatim from the single storage class this package replaced. The
text is what it was; only the module that owns it changed, so a statement
that reads these can import them without importing everything else.
"""

from __future__ import annotations

import json
from typing import Any

from cloud_agent_shared.sandbox_restore import MAX_RESTORE_ATTEMPTS
from cloud_agent_shared.shared_protocol import (
    FORK_HELPER,
    MID_TURN_INPUT_CAPABILITY_PREFIX,
    NATIVE_FORK_AT_ANCHOR_CAPABILITY,
    SHARED_SANDBOX_HELPER_PINS,
    WORKSPACE_CHAT_CAPABILITY,
)

NATIVE_FORK_CAPABILITY_JSON = json.dumps([NATIVE_FORK_AT_ANCHOR_CAPABILITY], separators=(",", ":"))
WORKSPACE_CHAT_CAPABILITY_JSON = json.dumps([WORKSPACE_CHAT_CAPABILITY], separators=(",", ":"))

# Host ``h`` ships the exact Agent CLI version group ``g``'s container runs:
# the version its drivers were built and tested against. A release that only
# rebuilds the sandbox image leaves this true. Every release does rebuild it —
# the image is labelled with its source commit — so asking whether the host
# still runs the group's very image would be false after every deployment.
HOST_SHIPS_GROUP_CLI = "(h.runtime_versions_available -> g.runtime_id) ? g.runtime_version"

# Whether the host holding group ``g`` can fork one of its members at a
# historical anchor. The host must advertise it, and the fork must run the way
# it was tested: the container's own fork helper understands the anchor (the
# original helper ignored it and silently forked the latest message), and the
# host drives the CLI version the container runs. A host running the very image
# the group was created from satisfies both, and is the only proof for a group
# whose helper version is unrecorded or outside the reviewed pin. A helper pin
# change therefore requires an explicit review of retained-group compatibility.
NATIVE_FORK_CAPABLE = f"""EXISTS (
    SELECT 1 FROM agent_runner_hosts h
    WHERE h.host = g.host
      AND h.status = 'online'
      AND h.capabilities @> '{NATIVE_FORK_CAPABILITY_JSON}'::jsonb
      AND (
          h.sandbox_image_digest = g.sandbox_image_digest
          OR (
              g.fork_helper_version = '{SHARED_SANDBOX_HELPER_PINS[FORK_HELPER]}'
              AND {HOST_SHIPS_GROUP_CLI}
          )
      )
)"""

# Whether a new chat can be created into group ``g`` now. Its host starts a
# chat's first session in a container another chat prepared, and that
# preparation is over: the chat that created the group has settled a turn, so
# it never prepares (and resets) the workspace again. The group must also be one
# a turn can be claimed into — its container exists, and nothing is stopping,
# deleting or moving it. ``create_workspace_chat`` refuses each of these with
# its own reason; this is the same answer for the page to read.
WORKSPACE_CHAT_CAPABLE = f"""(
    g.state IN ('starting', 'running', 'stopped')
    AND g.container_id IS NOT NULL
    AND g.stop_requested_at IS NULL
    AND g.deletion_state NOT IN ('pending', 'archived', 'deleted')
    AND EXISTS (
        SELECT 1 FROM agent_thread_sandbox_memberships origin
        WHERE origin.thread_id = g.origin_thread_id
          AND origin.sandbox_id = g.id
          AND origin.last_settled_job_id IS NOT NULL
    )
    AND EXISTS (
        SELECT 1 FROM agent_runner_hosts h
        WHERE h.host = g.host
          AND h.status = 'online'
          AND h.capabilities @> '{WORKSPACE_CHAT_CAPABILITY_JSON}'::jsonb
    )
)"""

# Every runnable chat is a member of a retained shared-live group: one
# ``agent_sandboxes`` row per container, one membership row per chat. This is
# the read shape the worker and owner routes see for a thread's sandbox.
THREAD_SANDBOX_SOURCE = f"""
(
    SELECT
        m.thread_id, g.id AS sandbox_id, g.generation, g.host, g.container_id,
        g.container_name, g.runtime_id, g.runtime_version,
        g.sandbox_image_digest, g.execution_profile, g.state,
        g.memory_limit, g.cpu_limit, g.disk_limit_mib, g.stop_timeout_seconds,
        m.context_status, m.activation_generation, m.process_generation,
        m.active_attempt_id, m.admitted_job_id, m.admitted_at,
        m.last_settled_job_id, m.native_session_id, g.idle_deadline,
        COALESCE(m.last_error, g.last_error) AS last_error,
        m.created_at, m.updated_at, m.fork_operation_id,
        TRUE AS shared_live, g.origin_thread_id, m.resident_state,
        m.residency_generation, m.resident_slot_id,
        g.resident_slot_capacity, m.native_state,
        m.forked_from_thread_id, m.fork_anchor_job_id,
        g.last_activity_at AS group_last_activity_at,
        (
            SELECT snapshot.state
            FROM agent_sandbox_snapshots snapshot
            WHERE snapshot.sandbox_id = g.id
            ORDER BY snapshot.workspace_epoch DESC, snapshot.id DESC
            LIMIT 1
        ) AS workspace_publication_state,
        -- The newest snapshot was published without a pull request: its
        -- capture found nothing to publish.
        (
            SELECT snapshot.state = 'published' AND snapshot.published_pr_url IS NULL
            FROM agent_sandbox_snapshots snapshot
            WHERE snapshot.sandbox_id = g.id
            ORDER BY snapshot.workspace_epoch DESC, snapshot.id DESC
            LIMIT 1
        ) AS workspace_publication_unchanged,
        (
            SELECT snapshot.published_pr_url
            FROM agent_sandbox_snapshots snapshot
            WHERE snapshot.sandbox_id = g.id AND snapshot.published_pr_url IS NOT NULL
            ORDER BY snapshot.workspace_epoch DESC, snapshot.id DESC
            LIMIT 1
        ) AS workspace_pr_url,
        -- Something the owner has not published: a turn settled since the
        -- last snapshot, or a last publication that failed. Turns settle in
        -- order and a publication takes all of them, so each member's newest
        -- settled turn answers for the ones before it. A Fork's copies of its
        -- source's turns ran in no workspace of their own, and a temporary
        -- workspace has nowhere to publish to.
        EXISTS (
            SELECT 1 FROM agent_threads published_to
            WHERE published_to.id = m.thread_id AND published_to.repo IS NOT NULL
        ) AND (
            EXISTS (
                SELECT 1 FROM agent_thread_sandbox_memberships settled
                JOIN agent_jobs settled_job ON settled_job.id = settled.last_settled_job_id
                WHERE settled.sandbox_id = g.id
                  AND settled_job.fork_source_job_id IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM agent_snapshot_jobs held
                      WHERE held.job_id = settled.last_settled_job_id
                  )
            )
            OR COALESCE((
                SELECT snapshot.state = 'failed'
                FROM agent_sandbox_snapshots snapshot
                WHERE snapshot.sandbox_id = g.id
                ORDER BY snapshot.workspace_epoch DESC, snapshot.id DESC
                LIMIT 1
            ), FALSE)
        ) AS workspace_unpublished_changes,
        g.workspace_epoch,
        {NATIVE_FORK_CAPABLE} AS native_fork_capable,
        {WORKSPACE_CHAT_CAPABLE} AS workspace_chat_capable,
        -- The host holding this group can hand a message to a running turn of
        -- the group's runtime: its sandbox_manager and driver say so, per runtime, and
        -- the driver speaks the CLI version this container runs. No helper in
        -- the container takes part; the message goes to the CLI itself.
        EXISTS (
            SELECT 1 FROM agent_runner_hosts h
            WHERE h.host = g.host
              AND h.status = 'online'
              AND h.capabilities @> jsonb_build_array(
                  '{MID_TURN_INPUT_CAPABILITY_PREFIX}' || g.runtime_id
              )
              AND (
                  h.sandbox_image_digest = g.sandbox_image_digest
                  OR {HOST_SHIPS_GROUP_CLI}
              )
        ) AS mid_turn_input_capable,
        COALESCE((
            SELECT jsonb_agg(
                jsonb_build_object(
                    'thread_id', peer.thread_id,
                    'native_session_id', peer.native_session_id,
                    'native_state', peer.native_state,
                    'context_status', peer.context_status,
                    'activation_generation', peer.activation_generation,
                    'process_generation', peer.process_generation,
                    'residency_generation', peer.residency_generation,
                    'resident_state', peer.resident_state,
                    'resident_slot_id', peer.resident_slot_id,
                    'active_attempt_id', peer.active_attempt_id,
                    'admitted_job_id', peer.admitted_job_id,
                    'admitted_at', peer.admitted_at,
                    'last_settled_job_id', peer.last_settled_job_id,
                    'forked_from_thread_id', peer.forked_from_thread_id,
                    'fork_anchor_job_id', peer.fork_anchor_job_id
                ) ORDER BY peer.created_at, peer.thread_id
            )
            FROM agent_thread_sandbox_memberships peer
            WHERE peer.sandbox_id = g.id
              -- Failed Fork placeholders and stopped deleted chats keep their
              -- rows but release their slots. Only current slot owners belong
              -- in the host manifest.
              AND peer.resident_slot_id IS NOT NULL
        ), '[]'::jsonb) AS group_members,
        g.start_seq,
        g.restore_attempts,
        -- What the claiming host recreates the group from. Only a group that
        -- a restore-capable host has claimed carries it: every other group
        -- either has its container or must not be created empty.
        (
            SELECT jsonb_build_object(
                'checkpoint_id', c.id,
                'object_key', c.object_key,
                'version_id', c.object_version_id,
                'sha256', c.sha256,
                'size_bytes', c.size_bytes,
                'base_image', c.base_image,
                'format_version', c.format_version
            )
            FROM agent_sandbox_checkpoints c
            WHERE g.state = 'restoring' AND c.id = g.restore_checkpoint_id
        ) AS restore
    FROM agent_thread_sandbox_memberships m
    JOIN agent_sandboxes g ON g.id = m.sandbox_id
) AS s
"""

# SET-clause fragment for ``agent_sandboxes``: count one more start the
# control plane let through and drop the restore point a start would make
# stale. A checkpoint receipt covers the group only while the sequence it
# carries is still current, so every path that may start the container takes
# this before it reaches the sandbox manager. A cold or restoring group keeps
# the checkpoint it is recreated from.
FENCE_START_SQL = (
    "start_seq = start_seq + 1, "
    "restore_checkpoint_id = CASE WHEN state IN ('cold', 'restoring') "
    "THEN restore_checkpoint_id ELSE NULL END"
)


async def fence_sandbox_start(conn: Any, sandbox_id: str) -> str | None:
    """Fence one group against starts and return its state.

    For callers that are about to ask a sandbox manager for something that may
    start a stopped container: a workspace request, a fork, a snapshot. The
    returned state lets them refuse a group that only a checkpoint holds.
    """
    return await conn.fetchval(
        f"""
        UPDATE agent_sandboxes
        SET {FENCE_START_SQL}, updated_at = NOW()
        WHERE id = $1
        RETURNING state
        """,
        sandbox_id,
    )


async def release_failed_restore(conn: Any, attempt_id: int, detail: str) -> str | None:
    """Return a group this attempt was recreating to its checkpoint.

    The claim that rehomes a cold group records its attempt. However that
    attempt ends before Docker confirmed the recreated container (handed back,
    finished, abandoned, or its lease expired), nothing was admitted and the
    checkpoint is untouched, so the group goes back to ``cold`` for another
    claim; a container the attempt may have left is fenced by the generation
    the next claim advances. Once the attempts run out it is ``lost`` instead.
    Returns the group's new state, or ``None`` when the attempt restored
    nothing.
    """
    # Memberships before their group, the order every writer keeps. The
    # restoring member may already hold the attempt from its reservation.
    await conn.execute(
        """
        UPDATE agent_thread_sandbox_memberships
        SET active_attempt_id = NULL, updated_at = NOW()
        WHERE active_attempt_id = $1
          AND sandbox_id IN (
              SELECT id FROM agent_sandboxes
              WHERE restore_attempt_id = $1 AND state = 'restoring'
          )
        """,
        attempt_id,
    )
    return await conn.fetchval(
        f"""
        UPDATE agent_sandboxes
        SET state = CASE
                WHEN restore_attempts >= {MAX_RESTORE_ATTEMPTS} THEN 'lost' ELSE 'cold'
            END,
            container_id = NULL, restore_attempt_id = NULL,
            last_error = $2, updated_at = NOW()
        WHERE restore_attempt_id = $1 AND state = 'restoring'
        RETURNING state
        """,
        attempt_id,
        detail,
    )


async def allocate_resident_slot(conn: Any, *, sandbox_id: str, capacity: int) -> int | None:
    """Choose a free resident slot in a group the caller holds locked.

    For a new member of an existing group: a Fork's child or a chat created
    into the workspace. Returns ``None`` when every slot is taken. The caller
    holds the group row, so two new members cannot be handed the same slot.

    A deleted chat keeps its membership as an audit row, so without the
    release below its slot stays occupied forever. The slot is a fixed member
    UID: it may serve a new member only once the old member can no longer act.
    Stopped/lost is the durable observation that its process is gone, and
    nothing restarts a deleted chat; a released residency means no grant still
    speaks for it. SKIP LOCKED keeps this transaction, which already holds the
    sandbox row, from waiting on a membership and inverting the membership ->
    sandbox order every other writer takes. A member skipped now is reclaimed
    by a later allocation.
    """
    await conn.execute(
        """
        UPDATE agent_thread_sandbox_memberships
        SET resident_slot_id = NULL, updated_at = NOW()
        WHERE thread_id IN (
            SELECT m.thread_id
            FROM agent_thread_sandbox_memberships m
            JOIN agent_threads t ON t.id = m.thread_id
            WHERE m.sandbox_id = $1
              AND m.resident_slot_id IS NOT NULL
              AND t.deleted_at IS NOT NULL
              AND m.resident_state IN ('stopped', 'lost')
              AND m.residency_status = 'none'
              AND m.active_attempt_id IS NULL
              AND m.fork_operation_id IS NULL
            FOR UPDATE OF m SKIP LOCKED
        )
        """,
        sandbox_id,
    )
    occupied_rows = await conn.fetch(
        """
        SELECT resident_slot_id AS slot
        FROM agent_thread_sandbox_memberships
        WHERE sandbox_id = $1 AND resident_slot_id IS NOT NULL
        UNION ALL
        SELECT target_resident_slot_id AS slot
        FROM agent_session_forks
        WHERE sandbox_id = $1 AND state IN ('preparing', 'materialized')
          AND target_resident_slot_id IS NOT NULL
        """,
        sandbox_id,
    )
    occupied = {int(item["slot"]) for item in occupied_rows}
    return next((slot for slot in range(capacity) if slot not in occupied), None)
