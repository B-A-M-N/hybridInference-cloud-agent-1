"""The supervisor admits a chat created into an existing group, and only that one.

A Fork's child is reserved in the database before its native session exists,
so a member with no session is left out of the manifest the host installs —
only the group's origin may start without one. A chat created into the
workspace is the other exception, named by its own acquire: it is installed,
leased, and opens a new session in its own slot of the container the group
already has. Nothing in that path creates a container.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from tests.unit.test_shared_session_supervisor import (
    _launch,
    _Manager,
    _members,
    _spec,
    _TransportFactory,
)

from cloud_agent_host.sandbox import SandboxError
from cloud_agent_host.shared_session_supervisor import (
    SharedChatSessionSupervisor,
    SharedMemberManifest,
)

_JOINED = SharedMemberManifest(
    thread_id="athr_joined",
    native_session_id=None,
    context_status="new",
    resident_slot_id=2,
    last_settled_job_id=None,
)


def _group() -> tuple[_Manager, _TransportFactory, SharedChatSessionSupervisor]:
    manager = _Manager()
    factory = _TransportFactory()
    supervisor = SharedChatSessionSupervisor(manager, transport_factory=factory)
    supervisor.register_manifest(_spec(), _members())
    return manager, factory, supervisor


def _acquire(supervisor: SharedChatSessionSupervisor, **overrides):
    params = {
        "thread_id": "athr_joined",
        "owner": "attempt:9",
        "activation_generation": 1,
        "has_settled_context": False,
        "manifest": [*_members(), _JOINED],
        "expected_container_id": "container-shared",
        "joins_workspace": True,
    }
    params.update(overrides)
    return supervisor.acquire(_spec(), **params)


def test_a_joining_chat_opens_a_new_session_in_the_existing_container():
    manager, factory, supervisor = _group()
    containers = dict(manager.items)

    lease = _acquire(supervisor)
    descriptor = supervisor.ensure_resident(
        "athr_joined", lease.token, _launch("athr_joined", native_session_id=None, slot=2)
    )

    assert lease.context_status == "new"
    assert manager.items == containers, "the group's own container, nothing created"
    assert [launch.slot_id for launch in factory.opened] == [2]
    assert factory.opened[0].native_session_id is None
    assert descriptor.resident_action == "new"
    assert descriptor.native_session_id == "native-child"
    # Its siblings are untouched: no lease, no process opened for them.
    assert supervisor.status("athr_source").resident_state != "live"


def test_without_joining_a_member_with_no_session_is_still_left_out():
    """The rule a pending Fork child relies on stays as it was."""
    _manager, _factory, supervisor = _group()

    with pytest.raises(SandboxError, match="no shared sandbox membership"):
        _acquire(supervisor, joins_workspace=False)


@pytest.mark.parametrize(
    "overrides",
    [
        # The origin prepares its group; it never joins it.
        {"thread_id": "athr_source"},
        # A member with a session or a settled turn resumes; it does not join.
        {
            "manifest": [
                *_members(),
                replace(_JOINED, native_session_id="native-x"),
            ]
        },
        {
            "manifest": [
                *_members(),
                replace(_JOINED, last_settled_job_id="ajob_1"),
            ]
        },
        {"has_settled_context": True},
        # The manifest must name the chat it is acquired for.
        {"manifest": _members()},
    ],
)
def test_only_a_new_non_origin_member_can_join(overrides):
    _manager, factory, supervisor = _group()

    with pytest.raises(ValueError, match="only a new non-origin member"):
        _acquire(supervisor, **overrides)
    assert factory.opened == []


def test_a_joining_chat_never_creates_its_group():
    """A sandbox manager that does not know the group refuses rather than create one."""
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager, transport_factory=_TransportFactory())

    with pytest.raises(SandboxError, match="needs that workspace's container"):
        _acquire(supervisor, expected_container_id=None)
    assert manager.items == {}
