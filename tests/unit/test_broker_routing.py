"""Requests reach the machine holding the worktree, not the configured one.

The bug this prevents is quiet, which is why it gets its own file. With one
`AGENT_WORKSPACE_BROKER_URL` and several hosts, a file listing for a job on
host B is answered by host A's broker: not an error, not a 500 — a plausible
directory listing of the wrong disk. Everything here is about which base URL a
request actually goes to.
"""

from __future__ import annotations

from typing import Any

import pytest

from cloud_agent.broker_routing import broker_for_attempt, broker_for_job

BROKER_TOKEN_ENV = "AGENT_WORKSPACE_BROKER_TOKEN"
BROKER_URL_ENV = "AGENT_WORKSPACE_BROKER_URL"


class FakeRoutingStore:
    """Answers only the two lookups broker routing performs."""

    def __init__(self, by_attempt: dict[int, dict[str, Any]] | None = None, **by_job: Any) -> None:
        self.by_attempt = by_attempt or {}
        self.by_job = by_job

    async def host_for_attempt(self, *, attempt_id: int) -> dict[str, Any] | None:
        return self.by_attempt.get(attempt_id)

    async def host_for_job(self, *, job_id: str) -> dict[str, Any] | None:
        return self.by_job.get(job_id)


def _base_url(client: Any) -> str:
    """The private base URL a broker client will actually address."""
    return client._base_url


@pytest.fixture(autouse=True)
def _broker_env(monkeypatch):
    monkeypatch.setenv(BROKER_TOKEN_ENV, "shared-broker-token")
    monkeypatch.delenv(BROKER_URL_ENV, raising=False)


async def test_two_hosts_two_brokers():
    """The whole point: each attempt's request goes to its own machine."""
    store = FakeRoutingStore(
        {
            1: {"host": "runner-a", "broker_url": "http://broker-a:8092"},
            2: {"host": "runner-b", "broker_url": "http://broker-b:8092"},
        }
    )

    first = await broker_for_attempt(store, 1)
    second = await broker_for_attempt(store, 2)

    assert _base_url(first) == "http://broker-a:8092"
    assert _base_url(second) == "http://broker-b:8092"


async def test_an_attempt_with_no_host_falls_back_to_the_single_host_env(monkeypatch):
    """E11's setup, and every job that predates the pool, must keep working."""
    monkeypatch.setenv(BROKER_URL_ENV, "http://legacy-broker:8092")
    store = FakeRoutingStore({})

    broker = await broker_for_attempt(store, 7)

    assert _base_url(broker) == "http://legacy-broker:8092"


async def test_no_attempt_at_all_falls_back_to_the_env(monkeypatch):
    monkeypatch.setenv(BROKER_URL_ENV, "http://legacy-broker:8092")

    assert (
        _base_url(await broker_for_attempt(FakeRoutingStore(), None)) == "http://legacy-broker:8092"
    )


async def test_a_host_without_a_broker_url_does_not_fall_back(monkeypatch):
    """The one fallback that would be actively wrong.

    Falling back here routes host B's request to host A's broker, and the
    result is not an error — it is an answer about the wrong machine's disk.
    Answering "no broker" surfaces as the 409 a deployment without one already
    produces, which is a question an operator can act on.
    """
    monkeypatch.setenv(BROKER_URL_ENV, "http://legacy-broker:8092")
    store = FakeRoutingStore({1: {"host": "runner-a", "broker_url": None}})

    assert await broker_for_attempt(store, 1) is None


async def test_a_host_with_a_blank_broker_url_is_treated_as_having_none(monkeypatch):
    monkeypatch.setenv(BROKER_URL_ENV, "http://legacy-broker:8092")
    store = FakeRoutingStore({1: {"host": "runner-a", "broker_url": "   "}})

    assert await broker_for_attempt(store, 1) is None


async def test_no_broker_token_means_no_broker(monkeypatch):
    """Fail closed rather than sending unauthenticated requests at a host."""
    monkeypatch.delenv(BROKER_TOKEN_ENV, raising=False)
    store = FakeRoutingStore({1: {"host": "runner-a", "broker_url": "http://broker-a:8092"}})

    assert await broker_for_attempt(store, 1) is None


async def test_the_job_lookup_routes_by_the_current_attempts_host():
    store = FakeRoutingStore(
        **{"job-1": {"host": "runner-b", "broker_url": "http://broker-b:8092"}}
    )

    broker = await broker_for_job(store, "job-1")

    assert _base_url(broker) == "http://broker-b:8092"


async def test_a_job_with_no_running_attempt_falls_back_to_the_env(monkeypatch):
    monkeypatch.setenv(BROKER_URL_ENV, "http://legacy-broker:8092")

    broker = await broker_for_job(FakeRoutingStore(), "job-unknown")

    assert _base_url(broker) == "http://legacy-broker:8092"


async def test_no_store_at_all_falls_back_to_the_env(monkeypatch):
    """An unconfigured deployment keeps whatever behaviour it had."""
    monkeypatch.setenv(BROKER_URL_ENV, "http://legacy-broker:8092")

    assert _base_url(await broker_for_attempt(None, 1)) == "http://legacy-broker:8092"
    assert _base_url(await broker_for_job(None, "job-1")) == "http://legacy-broker:8092"


async def test_every_broker_shares_the_one_token_today():
    """Recorded as a test because it is a limitation, not a design.

    Host rows carry a URL and no credential, so a compromised host's broker
    token opens every other host's broker. Making that per-host is a
    secret-distribution change (issue at enrollment, rotate, deliver), not a
    routing one. This test is here so the day it changes, it changes visibly.
    """
    store = FakeRoutingStore(
        {
            1: {"host": "runner-a", "broker_url": "http://broker-a:8092"},
            2: {"host": "runner-b", "broker_url": "http://broker-b:8092"},
        }
    )

    first = await broker_for_attempt(store, 1)
    second = await broker_for_attempt(store, 2)

    assert first._headers == second._headers
