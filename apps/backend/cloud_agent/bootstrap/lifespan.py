"""The control plane's internal assembly: resources, providers, lifecycle.

`app.py` keeps the two listeners and what they are made of; this module builds
what they need. It ran inline in `app.py` before, in the same order, setting the
same ``app.state`` names, and it still does — the move is about where the code
lives, not about when it runs. The database is required: a control plane without
one cannot claim, fence or record anything, so it refuses to start rather than
serving an API that fails at every call.
"""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from cloud_agent.logging_utils import get_logger
from cloud_agent.workers.attempts import reap_expired_attempts
from cloud_agent.workers.background_wake import BackgroundWake
from cloud_agent.workers.job_event_signal import JobEventSignal
from cloud_agent.workers.mcp_decision_signal import McpDecisionSignal
from cloud_agent.workers.notifications import listen
from cloud_agent.workers.owner_change_signal import OwnerChangeSignal
from cloud_agent.workers.sandbox_deletion import coordinate_sandbox_deletions
from cloud_agent.workers.snapshots import coordinate_shared_snapshots
from cloud_agent.workers.user_memory import refresh_user_memories
from cloud_agent.workers.work_signal import WorkSignal

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI

    from cloud_agent.publish_worker import PublishStorage
    from cloud_agent.workers.pr_archive import PrArchiveStorage

#: The moved lifespan keeps logging under the module it came from: the
#: deployment's log selection is on ``cloud_agent.app``.
logger = get_logger("cloud_agent.app")


ENV_DATABASE_URL = "AGENT_DATABASE_URL"


ENV_PUBLISH_BASE_BRANCH = "AGENT_PUBLISH_BASE_BRANCH"


ENV_GITHUB_TOKEN = "AGENT_GITHUB_TOKEN"


#: Held so the event loop cannot collect a running background task. Without
#: this the reaper and publisher are eligible for garbage collection the moment
#: nothing else references them, and they stop without a word.
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


def _build_source_control(app: FastAPI) -> None:
    """Attach the source-control runtime: GitHub App and GitLab OAuth.

    Each is optional and each failure is contained: a partial config disables
    that provider rather than booting with a credential path that cannot be
    secured. Disabled is not the same as absent, though, and a GitHub App that
    failed to load is loaded again (issue #47) — so what is attached is the
    runtime, never a client captured at startup.
    """
    from cloud_agent.source_control_runtime import SourceControlRuntime

    app.state.source_control = SourceControlRuntime.from_env()


def _start_publisher(app: FastAPI, store: PublishStorage) -> asyncio.Task[Any]:
    """Start the publish loop, idle if no git credential is configured."""
    from cloud_agent.publish_worker import GitHubCredential, publish_loop
    from cloud_agent.settings import setting_env

    def credential_provider() -> GitHubCredential | None:
        """Mint the credential for one publish, or None if unconfigured.

        Read per call rather than captured, so rotating the token does not
        need a restart.
        """
        token = setting_env(ENV_GITHUB_TOKEN, "")
        return GitHubCredential(token) if token else None

    task = asyncio.create_task(
        publish_loop(
            store,
            credential_provider=credential_provider,
            github_app=app.state.source_control.github,
            grants=app.state.grants,
            wake=getattr(app.state, "background_wake", None),
        )
    )
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    app.state.publish_task = task
    return task


def _start_pr_archiver(app: FastAPI, store: PrArchiveStorage) -> asyncio.Task[Any]:
    """Start durable PR merge checks with live source-control credentials."""
    from cloud_agent.publish_worker import GitHubCredential
    from cloud_agent.settings import setting_env
    from cloud_agent.workers.pr_archive import pr_archive_loop

    def credential_provider() -> GitHubCredential | None:
        """Read the optional deployment credential for each check."""
        token = setting_env(ENV_GITHUB_TOKEN, "")
        return GitHubCredential(token) if token else None

    task = asyncio.create_task(
        pr_archive_loop(
            store,
            credential_provider=credential_provider,
            github_app=app.state.source_control.github,
            client=app.state.http,
        )
    )
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    app.state.pr_archive_task = task
    return task


class _LifespanResources:
    """Resources acquired incrementally while one application lifespan starts."""

    def __init__(self) -> None:
        self.pool: Any = None
        self.http: Any = None
        self.source_control: Any = None
        self.measurement_writer: Any = None
        self.writer_registered = False
        self.tasks: list[asyncio.Task[Any]] = []

    def track_task(self, task: asyncio.Task[Any]) -> asyncio.Task[Any]:
        """Record a task immediately after it is created."""
        self.tasks.append(task)
        return task

    async def close(self, *, active_error: BaseException | None) -> None:
        """Release everything, then surface the first cleanup error if primary."""
        if self.pool is None:
            return

        from cloud_agent.api.mcp_relay import close_host
        from cloud_agent.broker_routing import close_broker_clients
        from cloud_agent.measurement_writer import reset_writer
        from cloud_agent.settings import reset_settings

        errors: list[BaseException] = []
        owner_task = asyncio.current_task()
        fatal_cancellation_recorded = False

        def record(name: str, exc: BaseException, *, fatal: bool = True) -> None:
            if fatal:
                errors.append(exc)
            logger.error(
                "cloud_agent_lifespan_cleanup_failed",
                extra={"event": "cloud_agent_lifespan_cleanup_failed", "resource": name},
                exc_info=(type(exc), exc, exc.__traceback__),
            )

        async def cleanup(name: str, operation: Any) -> None:
            try:
                await operation()
            except BaseException as exc:
                record(name, exc)

        if self.measurement_writer is not None:
            try:
                counts = await self.measurement_writer.shutdown()
                values = counts.as_dict()
                if any(values.get(name) for name in values if name != "reasons"):
                    logger.info(
                        "operation_measurement_writer_summary",
                        extra={"event": "operation_measurement_writer_summary", **values},
                    )
            except Exception as exc:
                # Measurement collection is best-effort and was already
                # non-fatal before this resource owner was introduced.
                record("measurement_writer", exc, fatal=False)
            except BaseException as exc:
                if isinstance(exc, asyncio.CancelledError):
                    fatal_cancellation_recorded = True
                record("measurement_writer", exc)
        if self.writer_registered:
            try:
                reset_writer(None)
            except BaseException as exc:
                record("measurement_writer_registry", exc)

        # Stop every task before awaiting any task. If one has already failed,
        # retrieving that failure cannot leave its siblings running.
        for task in self.tasks:
            try:
                task.cancel()
            except BaseException as exc:
                record("background_task_cancel", exc)
        for task in self.tasks:
            try:
                await task
            except asyncio.CancelledError as exc:
                if (
                    active_error is None
                    and not fatal_cancellation_recorded
                    and owner_task is not None
                    and owner_task.cancelling()
                ):
                    # Cancellation propagated from this cleanup task into the
                    # child it was joining; it is not the child's expected
                    # response to the cancel() above.
                    fatal_cancellation_recorded = True
                    record("lifespan_cleanup", exc)
            except BaseException as exc:
                record("background_task", exc)

        # Request-spawned send-now waits hold the same store, signal and broker
        # clients as the application. Their durable rows are the recovery
        # owner, so stop and join the eager waiters before those resources go.
        from cloud_agent.api.turns import close_send_now_handovers

        await cleanup("send_now_handovers", close_send_now_handovers)

        await cleanup("mcp_host", close_host)
        await cleanup("broker_clients", close_broker_clients)
        if self.source_control is not None:
            await cleanup("source_control", self.source_control.aclose)
        if self.http is not None:
            await cleanup("http", self.http.aclose)
        await cleanup("database_pool", self.pool.close)
        try:
            reset_settings()
        except BaseException as exc:
            record("settings_registry", exc)

        logger.info("cloud agent control plane stopped")
        if active_error is None and errors:
            raise errors[0]


@asynccontextmanager
async def lifespan(app: FastAPI, *, background: bool = True) -> AsyncIterator[None]:
    """Build the application while owning every resource from first acquisition."""
    resources = _LifespanResources()
    if background:
        context = _lifespan(app, background=True, resources=resources)
    else:
        context = _lifespan(app, background=False, resources=resources)
    try:
        async with context:
            yield
    finally:
        await resources.close(active_error=sys.exc_info()[1])


@asynccontextmanager
async def _lifespan(
    app: FastAPI, *, background: bool, resources: _LifespanResources
) -> AsyncIterator[None]:
    """Open the store, optionally start the reaper and publisher, tear down.

    Args:
        app: The application whose state is being built.
        background: Whether this process owns the reaper and the publisher.
            False for a second listener sharing the database — see the comment
            below for why that is not merely wasteful.
        resources: The owner that records each acquired resource for cleanup.

    The database is required. A control plane without one cannot claim, fence
    or record anything, so it refuses to start rather than serving an API that
    fails at every call.
    """
    import asyncpg
    import httpx

    from cloud_agent.api.mcp_relay import maintain_host_sessions
    from cloud_agent.application.profiling_control import ProfilingControlService
    from cloud_agent.capacity_history import prune_host_capacity_history
    from cloud_agent.gateway import GatewayClient
    from cloud_agent.grants import GrantClient
    from cloud_agent.ledger import collect_usage_snapshots
    from cloud_agent.mcp.catalog import McpCatalog
    from cloud_agent.operational_health import monitor_queue
    from cloud_agent.profiling_control_config import profiling_control_deployment
    from cloud_agent.settings import initialize_settings, setting_env, synchronize_settings
    from cloud_agent.storage.assembly import Storage
    from cloud_agent.workers.profiling_control import ProfilingPolicyWorker

    dsn = os.environ.get(ENV_DATABASE_URL, "")
    if not dsn:
        raise RuntimeError(f"{ENV_DATABASE_URL} must be set")

    pool = await asyncpg.create_pool(dsn)
    resources.pool = pool
    storage = Storage(pool)
    await initialize_settings(storage.settings, seed=background)
    # Schema is Alembic's. There is no initialize step to run: the store's
    # no-op one went with the store, and the DDL and backfills it used to
    # carry have been migrations since 0001.
    from cloud_agent.identity_namespace import ensure_gateway_namespace

    # An empty deployment must remain reachable through recovery-token setup.
    # A recorded foreign namespace still refuses startup before workers run.
    issuer = (setting_env("GATEWAY_ISSUER") or "").strip() or None
    app.state.gateway_issuer = await ensure_gateway_namespace(pool, issuer)

    app.state.storage = storage
    app.state.settings_task = resources.track_task(
        asyncio.create_task(synchronize_settings(storage.settings))
    )
    _BACKGROUND_TASKS.add(app.state.settings_task)
    app.state.settings_task.add_done_callback(_BACKGROUND_TASKS.discard)
    # One pool, shared by both gateway clients. Each call used to build its own
    # client and pay a connect — and a TLS handshake, where the gateway is
    # behind TLS — and admission is not an occasional call: `require_agent_owner`
    # is a dependency of every owner route, terminal input and resize included.
    # Both clients pass their own deadline per request, so this one carries no
    # default timeout of its own.
    http = httpx.AsyncClient()
    resources.http = http
    app.state.http = http
    app.state.gateway = GatewayClient(client=http)
    # The sandbox's credential is minted at the gateway now, so the claim
    # path and the renewal/revocation passes share that pool rather than each
    # building their own per call. They take the same seam; until this was
    # wired, the comment here described sharing that was not happening.
    app.state.grants = GrantClient(client=http)
    _build_source_control(app)
    resources.source_control = app.state.source_control

    # **The background work belongs to exactly one process.** The reaper
    # renews and revokes grants and supersedes lapsed attempts; the publisher
    # opens pull requests. A second listener on this same database — the MCP
    # relay is one — running its own copies would renew every grant twice per
    # interval, double the load it puts on the gateway, and have two workers
    # racing for the same publish.
    #
    # So this is a parameter of the lifespan rather than a property of it, and
    # `create_mcp_app` says no.
    app.state.mcp_catalog = None
    app.state.mcp_catalog_task = None
    app.state.compute_commands = None
    app.state.compute_resource_task = None
    if background:
        from cloud_agent.bootstrap.compute import build_compute_runtime

        app.state.compute_commands, compute_worker = build_compute_runtime(
            repository=storage.compute_resources,
            host_pool=storage.host_pool,
        )
        app.state.compute_resource_task = resources.track_task(
            asyncio.create_task(compute_worker.run())
        )
        _BACKGROUND_TASKS.add(app.state.compute_resource_task)
        app.state.compute_resource_task.add_done_callback(_BACKGROUND_TASKS.discard)
        app.state.mcp_catalog = McpCatalog(storage.mcp_catalog, http)
        app.state.mcp_catalog_task = resources.track_task(
            asyncio.create_task(app.state.mcp_catalog.run())
        )
        reaper = resources.track_task(
            asyncio.create_task(
                reap_expired_attempts(
                    storage,
                    grants=app.state.grants,
                    github_app=app.state.source_control.github,
                    gateway=app.state.gateway,
                )
            )
        )
        # The workers below wait on this as well as their clocks; the shared
        # listener started further down is what sets it.
        app.state.background_wake = BackgroundWake()
        snapshot_coordinator = resources.track_task(
            asyncio.create_task(
                coordinate_shared_snapshots(storage, wake=app.state.background_wake)
            )
        )
        sandbox_deletions = resources.track_task(
            asyncio.create_task(coordinate_sandbox_deletions(storage))
        )
        ledger_collector = resources.track_task(
            asyncio.create_task(
                collect_usage_snapshots(
                    storage.ledger, app.state.grants, wake=app.state.background_wake
                )
            )
        )
        # Host capacity history keeps filling whether or not anybody has the
        # admin page open — that is the whole reason it is stored at all rather
        # than accumulated in the browser. The points are written by the
        # heartbeats that accept them; what runs here is the retention that
        # bounds the table, in its own task, so a slow delete cannot delay a
        # heartbeat or the reaper.
        capacity_history = resources.track_task(
            asyncio.create_task(prune_host_capacity_history(storage.host_capacity))
        )
        user_memories = resources.track_task(
            asyncio.create_task(refresh_user_memories(storage.user_memory))
        )
        _BACKGROUND_TASKS.add(reaper)
        _BACKGROUND_TASKS.add(snapshot_coordinator)
        _BACKGROUND_TASKS.add(sandbox_deletions)
        _BACKGROUND_TASKS.add(ledger_collector)
        _BACKGROUND_TASKS.add(capacity_history)
        _BACKGROUND_TASKS.add(user_memories)
        reaper.add_done_callback(_BACKGROUND_TASKS.discard)
        snapshot_coordinator.add_done_callback(_BACKGROUND_TASKS.discard)
        sandbox_deletions.add_done_callback(_BACKGROUND_TASKS.discard)
        ledger_collector.add_done_callback(_BACKGROUND_TASKS.discard)
        capacity_history.add_done_callback(_BACKGROUND_TASKS.discard)
        user_memories.add_done_callback(_BACKGROUND_TASKS.discard)
        app.state.reaper_task = reaper
        app.state.snapshot_task = snapshot_coordinator
        app.state.sandbox_deletion_task = sandbox_deletions
        app.state.ledger_task = ledger_collector
        app.state.capacity_task = capacity_history
        app.state.user_memory_task = user_memories
        # The profiling policy loop runs in every control-plane process, not
        # only the one that owns the reaper: each process roots its own file and
        # terminal requests, so each one has to know what it is allowed to
        # collect. It polls the control row, which is why an idle deployment now
        # has a small, stated background cost instead of claiming zero I/O.
        profiling_service = ProfilingControlService(
            control=storage.profiling_control,
            deployment=profiling_control_deployment(),
            host_pool=storage.host_pool,
        )
        profiling_worker = ProfilingPolicyWorker(service=profiling_service)
        app.state.profiling_control_service = profiling_service
        app.state.profiling_policy_worker = profiling_worker
        app.state.profiling_policy_task = resources.track_task(
            asyncio.create_task(profiling_worker.run())
        )
        _BACKGROUND_TASKS.add(app.state.profiling_policy_task)
        app.state.profiling_policy_task.add_done_callback(_BACKGROUND_TASKS.discard)
        app.state.queue_monitor_task = resources.track_task(
            asyncio.create_task(monitor_queue(storage.host_pool))
        )
        _BACKGROUND_TASKS.add(app.state.queue_monitor_task)
        app.state.queue_monitor_task.add_done_callback(_BACKGROUND_TASKS.discard)
        # One connection to Postgres answers what waits on a change: idle hosts
        # parked on the worker wait endpoint (a job was queued, requeued or
        # ended), owners' event streams (their job stored an event or changed
        # state), owners' change streams (one of their jobs, conversations,
        # usage, queue, tool approvals or publications changed), and this
        # process's own snapshot, publish and usage workers.
        app.state.work_signal = WorkSignal()
        app.state.job_event_signal = JobEventSignal()
        app.state.owner_change_signal = OwnerChangeSignal()
        app.state.notification_listener_task = resources.track_task(
            asyncio.create_task(
                listen(
                    dsn,
                    (
                        app.state.work_signal,
                        app.state.job_event_signal,
                        app.state.owner_change_signal,
                        app.state.background_wake,
                    ),
                )
            )
        )
        _BACKGROUND_TASKS.add(app.state.notification_listener_task)
        app.state.notification_listener_task.add_done_callback(_BACKGROUND_TASKS.discard)
        # This listener holds no tool call for its owner's decision.
        app.state.mcp_decision_signal = None
        resources.track_task(_start_publisher(app, storage))
        resources.track_task(_start_pr_archiver(app, storage))
        logger.info("cloud agent control plane started")
    else:
        # The relay listener roots no file or terminal request, so it has no
        # configuration to apply and nothing to collect.
        app.state.profiling_control_service = None
        app.state.profiling_policy_worker = None
        app.state.profiling_policy_task = None
        app.state.reaper_task = None
        app.state.snapshot_task = None
        app.state.sandbox_deletion_task = None
        app.state.ledger_task = None
        app.state.capacity_task = None
        app.state.user_memory_task = None
        app.state.queue_monitor_task = None
        app.state.publish_task = None
        app.state.pr_archive_task = None
        app.state.work_signal = None
        app.state.job_event_signal = None
        app.state.owner_change_signal = None
        app.state.background_wake = None
        # The relay holds tool calls for their owners' decisions, and a
        # decision wakes the call it names: its own connection, on that one
        # channel.
        app.state.mcp_decision_signal = McpDecisionSignal()
        app.state.notification_listener_task = resources.track_task(
            asyncio.create_task(listen(dsn, (app.state.mcp_decision_signal,)))
        )
        _BACKGROUND_TASKS.add(app.state.notification_listener_task)
        app.state.notification_listener_task.add_done_callback(_BACKGROUND_TASKS.discard)
        logger.info("cloud agent relay listener started")

    # Each listener owns upstream sockets in its own process, even when it
    # does not run the control plane's grant reaper or publisher.
    app.state.mcp_maintenance_task = resources.track_task(
        asyncio.create_task(maintain_host_sessions(storage))
    )
    # Created per process, but worker tasks remain lazy until a selected
    # file/terminal record is actually offered.
    from cloud_agent.measurement_writer import MeasurementWriter, reset_writer

    resources.measurement_writer = MeasurementWriter()
    reset_writer(resources.measurement_writer)
    resources.writer_registered = True
    yield
