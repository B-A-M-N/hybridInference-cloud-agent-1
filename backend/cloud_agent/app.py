"""The control plane as its own service.

In the gateway this was forty lines inside a much larger ``bootstrap`` that
also built routers, adapters and observability. Here it is the whole process,
and the lifecycle is the same three things it always was: a store, a reaper,
and a publisher.

**The reaper is what makes a vanished sandbox recoverable.** It supersedes
attempts whose lease expired and requeues, fails or cancels the job per the
store's policy. It never dies on an error, because a transient database blip
must not permanently stop reaping — a stopped reaper is invisible until jobs
start hanging.

**The publisher is the only component holding a git credential**, which is
precisely what keeps the sandbox credential-free. A deployment without one
simply never publishes, rather than failing to start.

Both are background tasks held in a module-level set: a task the loop can
garbage-collect mid-run is a task that stops silently.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI

from cloud_agent.host_pool import HEARTBEAT_INTERVAL_S, MISSED_HEARTBEATS_BEFORE_OFFLINE
from cloud_agent.logging_utils import configure_process_logging, get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from cloud_agent.storage.store import AgentJobStore

logger = get_logger(__name__)

ENV_DATABASE_URL = "AGENT_DATABASE_URL"
ENV_PUBLISH_BASE_BRANCH = "AGENT_PUBLISH_BASE_BRANCH"
ENV_GITHUB_TOKEN = "AGENT_GITHUB_TOKEN"

#: Held so the event loop cannot collect a running background task. Without
#: this the reaper and publisher are eligible for garbage collection the moment
#: nothing else references them, and they stop without a word.
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()

#: How often the reaper looks for expired leases and stalled publishes.
REAP_INTERVAL_S = 30.0
MAX_ATTEMPTS = 3


async def _reconcile_settled_terminals(store: AgentJobStore) -> None:
    """Replay durable settled-terminal recovery markers after restarts.

    The broker is resolved per job, because with a host pool there is no
    "the" broker: each job's retained terminal lives on the machine that ran
    it. A single env-configured client here would resume one host's terminals
    and quietly report success for every other host's.
    """
    from cloud_agent.broker_routing import broker_for_job
    from cloud_agent.terminal_coordination import resume_settled_terminal

    for job_id in await store.list_terminal_resumes_pending():
        broker = await broker_for_job(store, job_id)
        if await resume_settled_terminal(job_id, broker=broker):
            await store.mark_terminal_resume_complete(job_id=job_id)


async def _revoke_dead_grants(store: AgentJobStore, grants: Any) -> None:
    """Withdraw the grants of attempts that are no longer current.

    Runs on the reaper's schedule rather than at the moment an attempt ends,
    and covers every way an attempt can stop being current with one query: a
    worker reporting a terminal state, the reaper superseding a lapsed lease,
    or a control plane that was restarted between the two. A revoke call at
    each of those call sites would have to be written three times, would put a
    gateway round trip inside the fenced terminal transition, and would still
    miss the restart.

    Revocation is an acceleration of the TTL, never the mechanism — the store
    detaches each grant as it hands it over, so a failure here costs lateness
    and not a retry every pass. ``GrantClient.revoke`` reports rather than
    raises for the same reason.
    """
    for row in await store.take_revocable_grants():
        if not await grants.revoke(row["grant_id"]):
            logger.warning(
                "agent_grant_not_revoked",
                extra={
                    "event": "agent_grant_not_revoked",
                    "job_id": row["job_id"],
                    "attempt_id": row["attempt_id"],
                },
            )


async def _renew_live_grants(store: AgentJobStore, grants: Any) -> None:
    """Extend the grants of attempts that are still current — and only those.

    **The store is consulted every pass, and that is the fence.** The gateway
    holds no attempt state after the split, so it cannot tell a live attempt
    from a superseded one; a renewal loop that remembered what it minted and
    renewed on a timer would keep a zombie sandbox spending, and nothing
    anywhere would notice. ``list_renewable_grants`` answers only for attempts
    that are ``running`` with an unexpired lease, so a superseded attempt stops
    being renewed on the very next pass and its capability dies at its own TTL.

    A refused renewal is logged, not retried here: the next pass tries again if
    the attempt is still live, and stops trying the moment it is not.
    """
    for row in await store.list_renewable_grants():
        try:
            await grants.renew(row["grant_id"])
        except Exception:
            logger.warning(
                "agent_grant_renewal_failed",
                exc_info=True,
                extra={
                    "event": "agent_grant_renewal_failed",
                    "job_id": row["job_id"],
                    "attempt_id": row["attempt_id"],
                },
            )


async def _reap_offline_hosts(store: AgentJobStore) -> None:
    """Move hosts that stopped heartbeating to ``offline``.

    Runs on the same loop as the attempt reaper rather than on its own, because
    the two are one recovery story told in order: a host disappears, it stops
    being claimable, and then the work it was holding comes back. Splitting
    them into two schedules would make the interval between them a race that
    only shows up under load.
    """
    offline = await store.reap_offline_hosts(
        interval_seconds=HEARTBEAT_INTERVAL_S,
        missed_intervals=MISSED_HEARTBEATS_BEFORE_OFFLINE,
    )
    if offline:
        logger.warning(
            "agent_hosts_offline",
            extra={"event": "agent_hosts_offline", "hosts": offline},
        )


async def reap_expired_attempts(
    store: AgentJobStore,
    *,
    interval_seconds: float = REAP_INTERVAL_S,
    max_attempts: int = MAX_ATTEMPTS,
    grants: Any | None = None,
) -> None:
    """Periodically close attempts whose lease expired.

    Args:
        store: The agent store.
        interval_seconds: Delay between passes.
        max_attempts: How many attempts a job gets before it fails for good.
        grants: The grant client, when this deployment mints grants. ``None``
            skips both grant passes entirely — the capabilities then expire at
            their own TTL, which is the same backstop revocation accelerates.
    """
    try:
        await _reconcile_settled_terminals(store)
    except Exception:
        logger.warning("Initial settled terminal reconciliation failed", exc_info=True)
    while True:
        await asyncio.sleep(interval_seconds)
        # Hosts first. A machine that stopped heartbeating should be out of the
        # claim gate before the pass that requeues its work, otherwise the
        # requeued job can be handed straight back to the host that just lost
        # it — the retry budget burns down while nothing moves.
        #
        # In its own try, though. Sharing one with the attempt reaper below
        # would let a host-reaping failure skip lease reaping and stalled
        # publishes for that pass, which is a strictly worse outage than the
        # one it was reporting: jobs stop being recovered at all.
        try:
            await _reap_offline_hosts(store)
        except Exception:
            logger.warning("Host reaper pass failed", exc_info=True)
        try:
            actions = await store.reap_expired(max_attempts=max_attempts)
            await _reconcile_settled_terminals(store)
            if actions:
                logger.info(
                    "agent_attempts_reaped",
                    extra={"event": "agent_attempts_reaped", "count": len(actions)},
                )
            # A job the publisher took and never finished is invisible to the
            # pass above: that scans running attempts, and this job's attempt
            # finished before publishing began.
            stalled = await store.reap_stalled_publishes()
            if stalled:
                logger.warning(
                    "agent_publishes_stalled",
                    extra={"event": "agent_publishes_stalled", "job_ids": stalled},
                )
        except Exception:
            logger.warning("Agent attempt reaper pass failed", exc_info=True)
        # After the reaper, so an attempt superseded in this very pass has its
        # grant withdrawn in the same one — and in its own try for the reason
        # the host reaper has one: a gateway having a bad minute must not stop
        # attempts being recovered.
        if grants is not None:
            try:
                await _revoke_dead_grants(store, grants)
            except Exception:
                logger.warning("Grant revocation pass failed", exc_info=True)
            try:
                await _renew_live_grants(store, grants)
            except Exception:
                logger.warning("Grant renewal pass failed", exc_info=True)


def _build_source_control(app: FastAPI) -> None:
    """Attach GitHub App and GitLab OAuth clients, where configured.

    Each is optional and each failure is contained: a partial config disables
    that provider rather than booting with a credential path that cannot be
    secured.
    """
    app.state.app_credentials = None
    app.state.gitlab_oauth = None

    try:
        from cloud_agent.github_app import AppConfig, GitHubAppCredentials

        app_config = AppConfig.from_env(dict(os.environ))
        if app_config is not None:
            app.state.app_credentials = GitHubAppCredentials(app_config)
    except Exception:
        logger.warning("GitHub App config present but unusable", exc_info=True)

    try:
        from cloud_agent.source_control import (
            GitLabOAuthClient,
            GitLabOAuthConfig,
            SourceControlCipher,
        )

        gitlab_config = GitLabOAuthConfig.from_env(dict(os.environ))
        if gitlab_config is not None:
            app.state.gitlab_oauth = GitLabOAuthClient(gitlab_config, cipher=SourceControlCipher())
    except Exception:
        logger.warning("GitLab OAuth config present but unusable", exc_info=True)


def _start_publisher(app: FastAPI, store: AgentJobStore) -> None:
    """Start the publish loop, idle if no git credential is configured."""
    from cloud_agent.publish_worker import GitHubCredential, publish_loop

    static_token = os.getenv(ENV_GITHUB_TOKEN, "")

    def credential_provider() -> GitHubCredential | None:
        """Mint the credential for one publish, or None if unconfigured.

        Read per call rather than captured, so rotating the token does not
        need a restart.
        """
        token = os.getenv(ENV_GITHUB_TOKEN, static_token)
        return GitHubCredential(token) if token else None

    task = asyncio.create_task(
        publish_loop(
            store,
            credential_provider=credential_provider,
            app_credentials=app.state.app_credentials,
            base_branch=os.getenv(ENV_PUBLISH_BASE_BRANCH, "dev"),
        )
    )
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    app.state.publish_task = task


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the store, start the reaper and publisher, and tear them down.

    The database is required. A control plane without one cannot claim, fence
    or record anything, so it refuses to start rather than serving an API that
    fails at every call.
    """
    import asyncpg

    from cloud_agent.gateway import GatewayClient
    from cloud_agent.grants import GrantClient
    from cloud_agent.storage.store import AgentJobStore

    dsn = os.environ.get(ENV_DATABASE_URL, "")
    if not dsn:
        raise RuntimeError(f"{ENV_DATABASE_URL} must be set")

    pool = await asyncpg.create_pool(dsn)
    store = AgentJobStore(pool)
    # Schema is Alembic's; this is a no-op kept so the call site is honest
    # about when the store becomes usable.
    await store.initialize()

    app.state.store = store
    app.state.gateway = GatewayClient()
    # The sandbox's credential is minted at the gateway now (E9), so the claim
    # path and the renewal/revocation passes share one client — and one
    # connection pool — rather than each building their own per call.
    app.state.grants = GrantClient()
    _build_source_control(app)

    reaper = asyncio.create_task(reap_expired_attempts(store, grants=app.state.grants))
    _BACKGROUND_TASKS.add(reaper)
    reaper.add_done_callback(_BACKGROUND_TASKS.discard)
    app.state.reaper_task = reaper

    _start_publisher(app, store)
    logger.info("cloud agent control plane started")

    try:
        yield
    finally:
        for task in (app.state.reaper_task, app.state.publish_task):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await pool.close()
        logger.info("cloud agent control plane stopped")


def create_app() -> FastAPI:
    """Build the control plane application."""
    configure_process_logging()

    from cloud_agent.api import admin_hosts, hosts, routes, session

    app = FastAPI(
        title="FreeInference Cloud Agent",
        description="Cloud coding agents on sandboxed hosts",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(routes.router)
    app.include_router(hosts.router)
    app.include_router(admin_hosts.router)
    # The web app's way in. Mounted alongside the others rather than inside
    # ``routes.py`` because it authenticates nothing on entry — it is the route
    # that *creates* the credential every other route requires.
    app.include_router(session.router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness only — deliberately not readiness.

        A readiness probe here would have to decide what a degraded control
        plane is (no gateway? no git credential?), and every answer to that
        belongs to whoever operates it.
        """
        return {"status": "ok"}

    return app
