"""Privileged actions revalidate the account against the gateway.

The session is a cache of an authorization decision the gateway made at
sign-in. Without something that re-asks, a user suspended or downgraded there
keeps this service's privileges until the cookie expires — which is what made
the seven-day session a real hole rather than a long one. Bounding it with the
TTL alone would mean a TTL of minutes, so the two halves are designed together:
a session measured in hours, and this.

**It hangs on the request method, not on a list of paths.** A list is a thing
the next route added quietly falls off; the last case here is the one that
notices if a mutating route is ever wired to a gate that does not revalidate.
Reads deliberately do not revalidate — a gateway outage should not close
everybody's open pages, and polling this surface should not amplify onto the
gateway.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from cloud_agent.api import routes as agent_jobs_router
from cloud_agent.api.deps import get_gateway_client
from cloud_agent.gateway import GatewayError

pytestmark = pytest.mark.asyncio

_OWNER = "user_1"


class _StatusGateway:
    """The gateway's user-status contract, answered in-process."""

    def __init__(self, record: dict[str, Any] | None = None, *, error: bool = False) -> None:
        self.record = record if record is not None else {"exists": True, "active": True}
        self.record.setdefault("role", "internal")
        self.error = error
        self.asked: list[str] = []

    async def user_status(self, user_id: str) -> dict[str, Any]:
        self.asked.append(user_id)
        if self.error:
            raise GatewayError("gateway unreachable: nope")
        return self.record


def _app(gateway: _StatusGateway) -> FastAPI:
    """Two routes that differ only by method, both behind the owner gate."""
    app = FastAPI()

    @app.get("/probe")
    async def read(user: dict[str, Any] = Depends(agent_jobs_router.require_agent_owner)):
        return {"role": user["role"]}

    @app.post("/probe")
    async def write(user: dict[str, Any] = Depends(agent_jobs_router.require_agent_owner)):
        return {"role": user["role"]}

    identity = {"user_id": _OWNER, "role": "internal", "authenticated": True}
    # Override the credential resolver, not the role gate: the gate is what is
    # under test.
    app.dependency_overrides[agent_jobs_router.authenticate_agent_owner] = lambda: identity
    app.dependency_overrides[get_gateway_client] = lambda: gateway
    return app


@pytest_asyncio.fixture()
async def client_for():
    async def build(gateway: _StatusGateway):
        transport = ASGITransport(app=_app(gateway))
        return AsyncClient(transport=transport, base_url="https://test")

    return build


async def test_a_write_by_a_suspended_account_is_refused(client_for):
    """The hole this closes: a valid session for an account that is not."""
    gateway = _StatusGateway({"exists": True, "active": False})
    async with await client_for(gateway) as client:
        response = await client.post("/probe")

    assert response.status_code == 403
    assert gateway.asked == [_OWNER]


async def test_a_read_by_the_same_account_still_works(client_for):
    """Reads stay a pure session check, on purpose.

    Revalidating them would put every page load behind the gateway's
    availability — and make an outage close everyone's open tabs — for a
    question the session already answers well enough to *display* things.
    """
    gateway = _StatusGateway({"exists": True, "active": False})
    async with await client_for(gateway) as client:
        response = await client.get("/probe")

    assert response.status_code == 200
    assert gateway.asked == [], "a read must not cost a gateway round trip"


async def test_an_unreachable_gateway_refuses_writes(client_for):
    """Fail closed: an authority that cannot be reached granted nothing."""
    gateway = _StatusGateway(error=True)
    async with await client_for(gateway) as client:
        response = await client.post("/probe")

    assert response.status_code == 503


async def test_a_downgraded_role_loses_the_surface(client_for):
    """A demotion at the gateway takes effect here without waiting for expiry."""
    gateway = _StatusGateway({"exists": True, "active": True, "role": "pro"})
    async with await client_for(gateway) as client:
        response = await client.post("/probe")

    assert response.status_code == 403


async def test_the_gateways_current_role_reaches_the_route(client_for):
    """And the fresh role is what the handler sees, not the session's copy.

    A promotion that only took effect on the *next* request would be a
    surprising kind of correct; more to the point, a route that reads
    ``user["role"]`` should read the same answer this gate just checked.
    """
    gateway = _StatusGateway({"exists": True, "active": True, "role": "admin"})
    async with await client_for(gateway) as client:
        response = await client.post("/probe")

    assert response.status_code == 200
    assert response.json()["role"] == "admin"


def _gates(route: Any) -> set[str]:
    """Every dependency function name reachable from a route."""
    names: set[str] = set()
    stack = list(getattr(route.dependant, "dependencies", []))
    while stack:
        dependant = stack.pop()
        call = getattr(dependant, "call", None)
        if call is not None:
            names.add(getattr(call, "__name__", ""))
        stack.extend(getattr(dependant, "dependencies", []))
    return names


#: What a machine-to-machine route authenticates with instead. These carry
#: their own capability credential and have no browser session behind them to
#: have gone stale, so revalidating a *user* on them would be checking the
#: wrong thing. Named by the call each route actually makes, not by URL prefix:
#: a prefix exempts whatever is put under it, which is the same "the next one
#: falls off the list" failure this test exists to catch.
MACHINE_GATES = frozenset({"_worker_claims", "verify_dispatcher_access", "verify_admin_access"})


async def test_every_mutating_route_on_the_owner_surface_revalidates():
    """No route gets to skip this by being added later.

    The method rule is only as good as its coverage: a mutating route wired to
    ``authenticate_agent_owner`` directly, or to nothing at all, is one that
    never re-asks.
    """
    mutating = {"POST", "PUT", "PATCH", "DELETE"}

    unguarded = []
    for route in agent_jobs_router.router.routes:
        methods = getattr(route, "methods", set()) or set()
        if not (methods & mutating):
            continue
        if "require_agent_owner" in _gates(route):
            continue
        # Worker routes verify their capability token in the handler rather
        # than through a dependency, so the dependency tree cannot see it.
        try:
            body = inspect.getsource(route.endpoint)
        except (OSError, TypeError):  # pragma: no cover - defensive
            body = ""
        if any(gate in body for gate in MACHINE_GATES):
            continue
        unguarded.append(f"{sorted(methods)} {route.path}")

    assert not unguarded, (
        "these mutating routes authenticate nothing that can go stale — wire "
        f"them to require_agent_owner or to a machine credential: {unguarded}"
    )
