"""Starting a new chat in a workspace another chat already uses.

The new chat joins that chat's sandbox group: one container, one
``/workspace``, and a native Agent CLI session of its own that begins with no
history. It is a new conversation in every other respect, so it passes the
checks a new task does — the model resolves, the effort is offered, the owner
may still reach the repository, and the tools, SSH servers, skills and uploads
it names are theirs — while everything the workspace already fixed (repository, base,
runtime, profile, environment, plugins) comes from the chat it joins.

The store decides, under the group's lock, whether the workspace can take
another member now; this flow maps its answer onto HTTP.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from fastapi import HTTPException

from cloud_agent.application.follow_ups import session_archived_error
from cloud_agent.application.job_admission import (
    _attachment_selection_error,
    _idempotency_conflict,
    _require_offered_effort,
    _require_resolvable_model,
    _resolve_attachment_selection,
    _resolve_mcp_bindings,
    _resolve_skill_selection,
)
from cloud_agent.attachments.models import AttachmentError
from cloud_agent.domain.requests import request_fingerprint
from cloud_agent.domain.ssh_servers import SshServerUnavailable
from cloud_agent.entitlement import RepoNotAllowed, require_entitled_repo
from cloud_agent.execution_profiles import RESERVED_EXECUTION_METADATA_KEYS
from cloud_agent.logging_utils import get_logger
from cloud_agent.schemas import BASE_REF_METADATA_KEY, ONCALL_METADATA_KEY

if TYPE_CHECKING:
    from cloud_agent.ports import (
        AttachmentRepository,
        ConversationCommands,
        McpConnectionRepository,
        SkillRepository,
        SourceControlRepository as SourceControlPort,
    )
    from cloud_agent.schemas import AgentWorkspaceChatCreate

logger = get_logger("cloud_agent.api.routes")


class _WorkspaceChatStorage(Protocol):
    """What creating a chat in an existing workspace reaches the store for."""

    attachments: AttachmentRepository
    conversation_commands: ConversationCommands
    mcp: McpConnectionRepository
    skills: SkillRepository
    source_control: SourceControlPort


#: The store's refusals, as the owner is told them. Every one is a 409: the
#: request was well formed and the chat exists, but its workspace cannot take
#: another chat now.
_REFUSALS: dict[str, tuple[str, str]] = {
    "workspace_unavailable": (
        "workspace_unavailable",
        "This chat's workspace is no longer available, so no other chat can join it.",
    ),
    "workspace_not_ready": (
        "workspace_not_ready",
        "This chat's workspace is not ready for another chat yet. Try again once its "
        "first turn has finished and its workspace is running or stopped.",
    ),
    "workspace_chat_unavailable": (
        "workspace_chat_unavailable",
        "The server holding this workspace cannot start another chat in it.",
    ),
    "resource_admission_blocked": (
        "workspace_chat_unavailable",
        "The server holding this workspace is not taking new chats right now.",
    ),
    "resident_capacity_exhausted": (
        "resident_capacity_exhausted",
        "This workspace already runs as many chats as it can hold. Delete one of "
        "them to make room.",
    ),
}


def _refusal(reason: str) -> HTTPException:
    error_type, message = _REFUSALS.get(reason, _REFUSALS["workspace_unavailable"])
    return HTTPException(
        status_code=409, detail={"error": {"type": error_type, "message": message}}
    )


async def create_workspace_chat_as(
    source: dict[str, Any],
    body: AgentWorkspaceChatCreate,
    user: dict[str, Any],
    *,
    store: _WorkspaceChatStorage,
    app_credentials: Any | None,
    gateway: Any,
    client_request_id: str | None = None,
) -> dict[str, Any]:
    """Create a chat in ``source``'s workspace for ``user``, or refuse it.

    ``source`` is the owner's read of any turn of the chat whose workspace the
    new chat joins (``_owned_job``), carrying its retained sandbox.

    Raises:
        HTTPException: 400 for a model or effort that is not offered, 403 for a
            repository the owner can no longer reach, 404 when the chat is
            gone, and 409 when its workspace cannot take another chat or the
            ``Idempotency-Key`` was used for a different submission.
    """
    fingerprint = (
        request_fingerprint(
            "workspace-chat",
            {"source_job_id": source["id"], **body.model_dump(mode="json")},
        )
        if client_request_id is not None
        else None
    )
    if client_request_id is not None:
        # A resend is answered before the checks below, as a new task's is:
        # each of them can change between two sends of one submission.
        replayed = await store.conversation_commands.replayed_submission(
            user_id=user["user_id"],
            client_request_id=client_request_id,
            fingerprint=fingerprint,
        )
        if replayed is not None:
            if replayed.get("error") == "client_request_conflict":
                raise _idempotency_conflict()
            return replayed
    # Advisory, so a closed chat costs no model lookup; the store asks both
    # again under the group's lock.
    if source.get("archived_at") is not None:
        raise session_archived_error()
    sandbox = source.get("thread_sandbox")
    if not isinstance(sandbox, dict) or sandbox.get("shared_live") is not True:
        # A first turn that has not reached its sandbox yet is preparing one.
        if source.get("conversation_state") in {"queued", "running"}:
            raise _refusal("workspace_not_ready")
        raise _refusal("workspace_unavailable")
    # The workspace's harness: one container runs one Agent CLI.
    runtime = str(sandbox.get("runtime_id") or source["runtime"])
    model = await _require_resolvable_model(body.model, gateway=gateway, user=user)
    reasoning_effort = await _require_offered_effort(
        body.reasoning_effort, runtime=runtime, model=model, gateway=gateway, user=user
    )
    try:
        # A temporary workspace has no repository to lose access to.
        if source["repo"] is not None:
            await require_entitled_repo(
                source["repo"],
                user["user_id"],
                store=store.source_control,
                app_credentials=app_credentials,
            )
    except RepoNotAllowed as exc:
        raise HTTPException(
            status_code=403,
            detail={"error": {"type": "repo_not_allowed", "message": str(exc)}},
        ) from exc
    metadata = dict(body.metadata or {})
    # Server-owned facts, as on a new task: caller metadata cannot claim them.
    metadata.pop(BASE_REF_METADATA_KEY, None)
    metadata.pop(ONCALL_METADATA_KEY, None)
    for key in RESERVED_EXECUTION_METADATA_KEYS:
        metadata.pop(key, None)
    # The branch the workspace was pinned from is the new chat's too.
    base_ref = (source.get("metadata") or {}).get(BASE_REF_METADATA_KEY)
    if isinstance(base_ref, str) and base_ref:
        metadata[BASE_REF_METADATA_KEY] = base_ref
    mcp_bindings = await _resolve_mcp_bindings(
        body.mcp_bindings, runtime=runtime, store=store.mcp, user_id=user["user_id"]
    )
    skills = await _resolve_skill_selection(
        store.skills, user_id=user["user_id"], skill_ids=body.skill_ids
    )
    attachments = await _resolve_attachment_selection(
        store.attachments, user_id=user["user_id"], attachment_ids=body.attachment_ids
    )
    try:
        job = await store.conversation_commands.create_workspace_chat(
            source_job_id=str(source["id"]),
            user_id=user["user_id"],
            task_prompt=body.task_prompt,
            model=model,
            reasoning_effort=reasoning_effort,
            metadata=metadata or None,
            mcp_bindings=mcp_bindings,
            skills=skills,
            attachments=attachments,
            goal=body.goal,
            client_request_id=client_request_id,
            client_request_fingerprint=fingerprint,
            ssh_server_ids=body.ssh_server_ids,
        )
    except SshServerUnavailable as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "type": "invalid_request",
                    "message": (
                        "Only your SSH servers that passed their check can be selected: "
                        f"{', '.join(exc.server_ids)}."
                    ),
                }
            },
        ) from exc
    except AttachmentError as exc:
        raise _attachment_selection_error(exc) from exc
    if job is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": {"type": "not_found", "message": f"No such agent job: {source['id']}"}
            },
        )
    error = job.get("error")
    if error == "client_request_conflict":
        raise _idempotency_conflict()
    if error == "session_archived":
        raise session_archived_error()
    if error:
        raise _refusal(str(error))
    logger.info(
        "agent_workspace_chat_created",
        extra={
            "event": "agent_workspace_chat_created",
            "job_id": job["id"],
            "thread_id": job.get("thread_id"),
            "source_job_id": source["id"],
            "runtime": runtime,
        },
    )
    return job
