"""Failure-injection coverage for control-plane lifespan resource ownership."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import asyncpg
import pytest
from fastapi import FastAPI

from cloud_agent.api import turns
from cloud_agent.app import lifespan
from cloud_agent.bootstrap.lifespan import _LifespanResources


@pytest.fixture
def lifespan_dependencies(monkeypatch):
    """Replace external work while retaining the lifespan's real ownership."""
    monkeypatch.setenv("AGENT_DATABASE_URL", "postgresql://test-only")
    monkeypatch.delenv("GATEWAY_BASE_URL", raising=False)
    monkeypatch.delenv("GATEWAY_ISSUER", raising=False)

    pool = AsyncMock()
    monkeypatch.setattr(asyncpg, "create_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr("cloud_agent.settings.initialize_settings", AsyncMock())
    monkeypatch.setattr(
        "cloud_agent.identity_namespace.ensure_gateway_namespace",
        AsyncMock(return_value=None),
    )

    async def idle(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr("cloud_agent.settings.synchronize_settings", idle)
    monkeypatch.setattr("cloud_agent.bootstrap.lifespan.listen", idle)
    monkeypatch.setattr("cloud_agent.api.mcp_relay.maintain_host_sessions", idle)

    close_host = AsyncMock()
    close_brokers = AsyncMock()
    monkeypatch.setattr("cloud_agent.api.mcp_relay.close_host", close_host)
    monkeypatch.setattr("cloud_agent.broker_routing.close_broker_clients", close_brokers)

    source_control = SimpleNamespace(github=None, aclose=AsyncMock())

    def attach_source_control(app):
        app.state.source_control = source_control

    monkeypatch.setattr(
        "cloud_agent.bootstrap.lifespan._build_source_control",
        attach_source_control,
    )
    return SimpleNamespace(
        pool=pool,
        close_host=close_host,
        close_brokers=close_brokers,
        source_control=source_control,
    )


@pytest.mark.parametrize(
    ("stage", "background"),
    [
        ("storage", False),
        ("settings", False),
        ("namespace", False),
        ("http", False),
        ("source_control", False),
        ("compute", True),
        ("measurement_writer", False),
    ],
)
async def test_partial_startup_releases_every_acquired_resource(
    monkeypatch, lifespan_dependencies, stage, background
):
    """No fallible startup stage sits outside the resource owner's cleanup."""
    failure = RuntimeError(f"{stage} failed")
    if stage == "storage":
        monkeypatch.setattr(
            "cloud_agent.storage.assembly.Storage",
            Mock(side_effect=failure),
        )
    elif stage == "settings":
        monkeypatch.setattr(
            "cloud_agent.settings.initialize_settings",
            AsyncMock(side_effect=failure),
        )
    elif stage == "namespace":
        monkeypatch.setattr(
            "cloud_agent.identity_namespace.ensure_gateway_namespace",
            AsyncMock(side_effect=failure),
        )
    elif stage == "http":
        monkeypatch.setattr("httpx.AsyncClient", Mock(side_effect=failure))
    elif stage == "source_control":
        monkeypatch.setattr(
            "cloud_agent.bootstrap.lifespan._build_source_control",
            Mock(side_effect=failure),
        )
    elif stage == "compute":
        monkeypatch.setattr(
            "cloud_agent.bootstrap.compute.build_compute_runtime",
            Mock(side_effect=failure),
        )
        lifespan_dependencies.source_control.aclose.side_effect = RuntimeError(
            "source-control cleanup also failed"
        )
    else:
        monkeypatch.setattr(
            "cloud_agent.measurement_writer.MeasurementWriter",
            Mock(side_effect=failure),
        )

    app = FastAPI()
    with pytest.raises(RuntimeError) as caught:
        async with lifespan(app, background=background):
            pytest.fail("A failed startup stage must not begin serving")

    assert caught.value is failure
    lifespan_dependencies.pool.close.assert_awaited_once()
    lifespan_dependencies.close_host.assert_awaited_once()
    lifespan_dependencies.close_brokers.assert_awaited_once()

    if hasattr(app.state, "http"):
        assert app.state.http.is_closed
    if hasattr(app.state, "settings_task"):
        assert app.state.settings_task.cancelled()
    if stage in {"compute", "measurement_writer"}:
        lifespan_dependencies.source_control.aclose.assert_awaited_once()
    else:
        lifespan_dependencies.source_control.aclose.assert_not_awaited()

    for value in app.state._state.values():
        if isinstance(value, asyncio.Task):
            assert value.done()


async def test_failed_background_task_cannot_skip_later_cleanup(monkeypatch, lifespan_dependencies):
    """A task error remains primary after every later resource is released."""
    listener_failure = RuntimeError("listener died")

    async def failed_listener(*args, **kwargs):
        raise listener_failure

    monkeypatch.setattr(
        "cloud_agent.bootstrap.lifespan.listen",
        failed_listener,
    )
    lifespan_dependencies.close_host.side_effect = RuntimeError("host cleanup also failed")

    app = FastAPI()
    with pytest.raises(RuntimeError) as caught:
        async with lifespan(app, background=False):
            await asyncio.sleep(0)
            assert app.state.notification_listener_task.done()

    assert caught.value is listener_failure
    assert app.state.settings_task.cancelled()
    assert app.state.mcp_maintenance_task.cancelled()
    lifespan_dependencies.close_host.assert_awaited_once()
    lifespan_dependencies.close_brokers.assert_awaited_once()
    lifespan_dependencies.source_control.aclose.assert_awaited_once()
    assert app.state.http.is_closed
    lifespan_dependencies.pool.close.assert_awaited_once()


async def test_shutdown_joins_request_handoffs_before_shared_resources_close(
    lifespan_dependencies,
):
    """An eager send-now waiter cannot outlive the store and broker it uses."""
    started = asyncio.Event()
    joined = asyncio.Event()

    async def pending_handoff() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            joined.set()

    async def assert_handoff_joined() -> None:
        assert joined.is_set()

    lifespan_dependencies.close_brokers.side_effect = assert_handoff_joined
    lifespan_dependencies.pool.close.side_effect = assert_handoff_joined

    app = FastAPI()
    async with lifespan(app, background=False):
        task = asyncio.create_task(pending_handoff())
        turns._hand_overs.add(task)
        task.add_done_callback(turns._hand_overs.discard)
        await started.wait()

    assert task.cancelled()
    assert joined.is_set()
    assert not turns._hand_overs
    lifespan_dependencies.close_brokers.assert_awaited_once()
    lifespan_dependencies.pool.close.assert_awaited_once()


async def test_cancellation_during_handoff_join_finishes_before_shared_cleanup(
    lifespan_dependencies,
):
    """Owner cancellation cannot interrupt the handoff join or resource order."""
    started = asyncio.Event()
    task_cancelled = asyncio.Event()
    release = asyncio.Event()
    joined = asyncio.Event()

    async def slow_handoff_cleanup() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            task_cancelled.set()
            await release.wait()
        finally:
            joined.set()

    async def assert_handoff_joined() -> None:
        assert joined.is_set()

    handoff = asyncio.create_task(slow_handoff_cleanup())
    turns._hand_overs.add(handoff)
    handoff.add_done_callback(turns._hand_overs.discard)
    await started.wait()

    resources = _LifespanResources()
    resources.pool = lifespan_dependencies.pool
    lifespan_dependencies.close_brokers.side_effect = assert_handoff_joined
    lifespan_dependencies.pool.close.side_effect = assert_handoff_joined

    cleanup = asyncio.create_task(resources.close(active_error=None))
    await task_cancelled.wait()
    cleanup.cancel()
    await asyncio.sleep(0)
    assert not lifespan_dependencies.close_brokers.called
    assert not lifespan_dependencies.pool.close.called

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await cleanup

    assert joined.is_set()
    assert handoff.done()
    assert not turns._hand_overs
    lifespan_dependencies.close_brokers.assert_awaited_once()
    lifespan_dependencies.pool.close.assert_awaited_once()


async def test_measurement_shutdown_cancellation_propagates_after_later_cleanup(
    monkeypatch, lifespan_dependencies
):
    """Best-effort measurement cleanup cannot consume process cancellation."""
    writer = SimpleNamespace(shutdown=AsyncMock(side_effect=asyncio.CancelledError()))
    monkeypatch.setattr(
        "cloud_agent.measurement_writer.MeasurementWriter",
        Mock(return_value=writer),
    )

    app = FastAPI()
    with pytest.raises(asyncio.CancelledError):
        async with lifespan(app, background=False):
            pass

    assert app.state.settings_task.cancelled()
    assert app.state.notification_listener_task.cancelled()
    assert app.state.mcp_maintenance_task.cancelled()
    lifespan_dependencies.close_host.assert_awaited_once()
    lifespan_dependencies.close_brokers.assert_awaited_once()
    lifespan_dependencies.source_control.aclose.assert_awaited_once()
    assert app.state.http.is_closed
    lifespan_dependencies.pool.close.assert_awaited_once()


async def test_cancellation_while_joining_a_child_propagates_after_cleanup(
    lifespan_dependencies,
):
    """Owner cancellation is distinct from the child's expected cancellation."""
    started = asyncio.Event()
    first_cancel_seen = asyncio.Event()
    never = asyncio.Event()

    async def ignore_first_cancel():
        started.set()
        try:
            await never.wait()
        except asyncio.CancelledError:
            first_cancel_seen.set()
            await never.wait()

    child = asyncio.create_task(ignore_first_cancel())
    await started.wait()

    resources = _LifespanResources()
    resources.pool = lifespan_dependencies.pool
    resources.http = SimpleNamespace(aclose=AsyncMock())
    resources.source_control = lifespan_dependencies.source_control
    resources.track_task(child)

    cleanup = asyncio.create_task(resources.close(active_error=None))
    await first_cancel_seen.wait()
    cleanup.cancel()

    with pytest.raises(asyncio.CancelledError):
        await cleanup

    assert child.cancelled()
    lifespan_dependencies.close_host.assert_awaited_once()
    lifespan_dependencies.close_brokers.assert_awaited_once()
    lifespan_dependencies.source_control.aclose.assert_awaited_once()
    resources.http.aclose.assert_awaited_once()
    lifespan_dependencies.pool.close.assert_awaited_once()


async def test_primary_error_prevents_duplicate_owner_cancellation_record(
    lifespan_dependencies,
):
    """A second cancellation cannot replace the exception already unwinding."""
    primary = RuntimeError("primary")
    started = asyncio.Event()
    first_cancel_seen = asyncio.Event()
    never = asyncio.Event()

    async def ignore_first_cancel():
        started.set()
        try:
            await never.wait()
        except asyncio.CancelledError:
            first_cancel_seen.set()
            await never.wait()

    child = asyncio.create_task(ignore_first_cancel())
    await started.wait()
    resources = _LifespanResources()
    resources.pool = lifespan_dependencies.pool
    resources.track_task(child)

    async def unwind_primary():
        try:
            raise primary
        finally:
            await resources.close(active_error=primary)

    cleanup = asyncio.create_task(unwind_primary())
    await first_cancel_seen.wait()
    cleanup.cancel()

    with pytest.raises(RuntimeError) as caught:
        await cleanup

    assert caught.value is primary
    assert child.cancelled()
    lifespan_dependencies.pool.close.assert_awaited_once()
