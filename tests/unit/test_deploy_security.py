"""Security properties of the standalone deployment.

The source repo's whole `test_agent_deploy_config.py` is ported as
`test_deploy_config.py`, re-pointed at the standalone compose. This file holds
only what that port could not carry: assertions the standalone topology needs
that the overlay never had, and the record of what did not port.

The Kata preflight lives in `test_setup_kata_runtime.py`, which drives the
real script.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
COMPOSE_PATH = REPO / "deploy/host/docker-compose.agent-remote-runner.yml"
SANDBOX_DOCKERFILE = REPO / "deploy/sandbox/Dockerfile.agent-sandbox"
CODEX_REQUIREMENTS = REPO / "deploy/sandbox/codex-requirements.toml"
WORKFLOW = REPO / ".github/workflows/agent-job-runner.yml"


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


def _mounts(service: dict) -> list[str]:
    """Volume entries as strings, whatever syntax the compose file uses."""
    out = []
    for volume in service.get("volumes") or []:
        if isinstance(volume, str):
            out.append(volume)
        else:  # long syntax
            ro = ":ro" if volume.get("read_only") else ""
            out.append(f"{volume.get('source', '')}:{volume.get('target', '')}{ro}")
    return out


# ---------------------------------------------------------------------------
# The network boundary
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The runner's privileges
# ---------------------------------------------------------------------------


def test_every_docker_socket_holder_is_known_and_read_only(compose: dict) -> None:
    """The socket is the host's keys — the holder list is pinned, not open.

    Two services legitimately hold it: the runner starts each sandbox, and the
    broker execs into a running one to serve terminals. Both read-only. This
    asserts the *set*, so a third service acquiring the socket fails here
    rather than being noticed later, and it asserts read-only per holder rather
    than only for the runner.
    """
    holders = {
        name: [m for m in _mounts(svc) if "docker.sock" in m]
        for name, svc in compose["services"].items()
        if any("docker.sock" in m for m in _mounts(svc))
    }
    assert set(holders) == {"agent-runner", "agent-workspace-broker"}, sorted(holders)
    for name, mounts in holders.items():
        assert all(m.endswith(":ro") for m in mounts), f"{name}: {mounts}"


def test_the_sandbox_image_is_required_not_defaulted(compose: dict) -> None:
    """A defaulted image name is how a host silently runs something else."""
    assert ":?" in compose["services"]["agent-runner"]["environment"]["AGENT_SANDBOX_IMAGE"]


# ---------------------------------------------------------------------------
# The sandbox image
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# What did not port
# ---------------------------------------------------------------------------


def test_the_dropped_assertions_stay_dropped_deliberately(compose: dict) -> None:
    """Two source assertions do not port, and this records why.

    `test_the_gateway_is_reachable_from_the_sandbox_network` and the
    backend-service egress-network assertion both keyed on the gateway's own
    `backend:` service. In this topology the gateway is not a service at all —
    it is reached through the tunnel — so there is nothing to assert against.
    If a `backend` service ever appears here, that reasoning has expired and
    those assertions need rewriting rather than staying dropped.
    """
    assert "backend" not in compose["services"]
