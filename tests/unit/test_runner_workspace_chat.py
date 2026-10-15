"""A chat created into an existing workspace prepares nothing on its first turn.

Its group's container already holds the repository, the setup's output and the
files every other member is working on. The first turn of the chat that created
the group prepares that workspace — reset, checkout, setup, permissions and the
initial archive — and a retry of it prepares it again. A chat that joined later
must do none of it: it asks the sandbox manager for a new member of the group,
opens a native session of its own and records the one the CLI reports.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from tests.unit.test_runner_host_settings import _JOB, _stamp

from cloud_agent_host import runner as runner_mod
from cloud_agent_host.chat_sandbox import ChatSandboxSpec
from cloud_agent_host.runtimes import get_runtime
from cloud_agent_host.sandbox import ContainerBackend
from cloud_agent_host.session_supervisor_client import SessionSupervisorClient
from cloud_agent_host.setup import SetupResult

_ORIGIN = "athr_origin"


def _run_first_turn(monkeypatch, *, thread_id: str) -> SimpleNamespace:
    """Run a first turn of ``thread_id`` in a group created by ``_ORIGIN``."""
    backend = ContainerBackend(image="img")
    monkeypatch.setattr(backend, "version_stamp", _stamp)
    manager = Mock()
    manager.backend = backend
    retained = Mock()
    retained.sandbox_metadata.return_value = {}
    retained.has_binary.return_value = True
    monkeypatch.setattr(runner_mod, "ChatSandboxManager", lambda _backend: manager)
    monkeypatch.setattr(runner_mod, "RetainedChatBackend", lambda *_args, **_kwargs: retained)
    monkeypatch.setattr(runner_mod, "_resident_base_ready", lambda *_args: True)
    monkeypatch.setattr(runner_mod, "runtime_version_unavailable", lambda *_args: None)
    source = Mock()
    source.import_into.return_value = _JOB.base_sha
    prefetch = Mock(return_value=source)
    monkeypatch.setattr(runner_mod, "_prefetch_retained_source", prefetch)
    setup = Mock(return_value=SetupResult(ran=True))
    monkeypatch.setattr(runner_mod, "run_setup", setup)

    spec = ChatSandboxSpec(
        thread_id=_ORIGIN, sandbox_id="asbx_shared", generation=1, runtime_id="claude-code"
    )
    group = {
        "shared_live": True,
        "sandbox_id": spec.sandbox_id,
        "origin_thread_id": _ORIGIN,
        "generation": 1,
        "host": "host-a",
        "container_name": spec.container_name,
        "container_id": "the-container",
        "runtime_id": "claude-code",
        "runtime_version": _JOB.runtime_version,
        "sandbox_image_digest": "sha256:shared-image",
        "execution_profile": "trusted-v1",
        "resident_slot_capacity": 8,
    }
    control = Mock(spec=runner_mod.ControlPlane)
    control.reserve_chat_sandbox.side_effect = lambda seed: {
        **group,
        **seed,
        "thread_id": thread_id,
        "activation_generation": 1,
        "resident_slot_id": 0 if thread_id == _ORIGIN else 2,
        "native_session_id": None,
        "last_settled_job_id": None,
    }
    control.mark_chat_sandbox_running.return_value = {}
    control.establish_shared_residency.return_value = {
        "credential": "grant",
        "generation": 1,
        "model": _JOB.model,
    }
    supervisor = Mock()
    supervisor.acquire_shared.return_value = {
        "token": "lease",
        "container_id": "the-container",
        "activation_generation": 1,
        "context_status": "new",
        "resident_state": "starting",
    }
    supervisor.initial_archive_status.return_value = {
        "enabled": False,
        "prepared": False,
        "stored": False,
    }
    supervisor.capture_initial_archive.return_value = {"enabled": False, "stored": False}
    supervisor.ensure_shared_resident.return_value = {
        "context_status": "new",
        "process_generation": 1,
    }
    supervisor.settle_shared.return_value = {"native_session_id": "native-new"}
    monkeypatch.setattr(runner_mod, "_session_supervisor_from_env", lambda: supervisor)
    monkeypatch.setattr(
        runner_mod, "_stream_shared_agent_turn", lambda **_kwargs: (0, "", [], None, None)
    )
    monkeypatch.setenv("AGENT_CHAT_IDLE_TIMEOUT_S", "1800")
    finish = Mock()
    result = runner_mod._run_shared_live_attempt(
        runtime=get_runtime("claude-code"),
        job=replace(
            _JOB,
            thread_id=thread_id,
            setup_script="npm ci",
            thread_sandbox=group,
        ),
        control=control,
        heart=SimpleNamespace(cancel_requested=False, lease_lost=False),
        backend=backend,
        host="host-a",
        gateway_base_url="http://gateway",
        agent_timeout_s=30,
        finish_attempt=finish,
    )
    return SimpleNamespace(
        result=result,
        finish=finish,
        manager=manager,
        supervisor=supervisor,
        control=control,
        prefetch=prefetch,
        setup=setup,
    )


def test_a_joining_chat_opens_a_session_and_leaves_the_workspace_alone(monkeypatch):
    turn = _run_first_turn(monkeypatch, thread_id="athr_joined")

    assert turn.result == 0
    assert turn.finish.call_args.args[0] == "succeeded"
    acquire = turn.supervisor.acquire_shared.call_args
    assert acquire.kwargs["joins_workspace"] is True
    assert acquire.kwargs["has_settled_context"] is False
    # The group's container, found as it is; nothing created or replaced.
    assert acquire.kwargs["expected_container_id"] == "the-container"
    # No clone, reset, checkout, setup, permission pass or initial archive.
    turn.prefetch.assert_not_called()
    turn.setup.assert_not_called()
    turn.manager.reset_unsettled_state.assert_not_called()
    turn.manager.prepare_shared_workspace.assert_not_called()
    turn.supervisor.initial_archive_status.assert_not_called()
    turn.supervisor.capture_initial_archive.assert_not_called()
    # A new native session in this member's own slot, recorded as the CLI reports it.
    launch = turn.supervisor.ensure_shared_resident.call_args.kwargs["launch"]
    assert launch.native_session_id is None
    assert launch.slot_id == 2
    turn.control.report_context_status.assert_called_once_with("new")
    checked_out = [
        call.args[0].payload
        for call in turn.control.append_event.call_args_list
        if call.args[0].payload.get("phase") == "checked_out"
    ]
    assert checked_out == [
        {**checked_out[0], "reused": True, "joined": True, "workspace_shared": True}
    ]
    settled = turn.control.settle_chat_sandbox.call_args.kwargs
    assert settled["native_session_id"] == "native-new"
    assert settled["context_status"] == "new"


def test_the_chat_that_created_the_group_still_prepares_it(monkeypatch):
    """A retry of the origin's first turn is the one turn that prepares."""
    turn = _run_first_turn(monkeypatch, thread_id=_ORIGIN)

    assert turn.result == 0
    assert turn.supervisor.acquire_shared.call_args.kwargs["joins_workspace"] is False
    turn.prefetch.assert_called_once()
    turn.manager.reset_unsettled_state.assert_called_once()
    turn.setup.assert_called_once()
    turn.manager.prepare_shared_workspace.assert_called_once()
    turn.supervisor.capture_initial_archive.assert_called_once()


@pytest.mark.parametrize("joins", [False, True])
def test_only_a_joining_acquire_names_itself_to_the_sandbox_manager(joins):
    """An older sandbox manager never sees the field unless a chat is joining."""
    client = SessionSupervisorClient("http://sandbox-manager", "token")
    sent: list[dict[str, Any]] = []

    def request(_method: str, _thread_id: str, _action: str, payload: dict[str, Any]) -> dict:
        sent.append(payload)
        return {}

    client._shared_request = request  # type: ignore[method-assign]
    spec = ChatSandboxSpec(
        thread_id=_ORIGIN, sandbox_id="asbx_shared", generation=1, runtime_id="claude-code"
    )

    client.acquire_shared(
        spec,
        thread_id="athr_joined",
        activation_generation=1,
        owner="attempt:7",
        has_settled_context=False,
        members=[],
        joins_workspace=joins,
    )
    client.close()

    assert ("joins_workspace" in sent[0]) is joins
