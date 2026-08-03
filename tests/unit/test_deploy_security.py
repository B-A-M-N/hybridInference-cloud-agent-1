"""Security properties of the standalone deployment.

These were pinned in the source repo by `tests/unit/test_agent_deploy_config.py`,
which asserts on the co-located overlay compose. That topology is not migrating
(MIGRATION.md, Open decisions), and dropping a topology must not drop its
guarantees — so every property that still exists is re-asserted here against
`deploy/host/docker-compose.agent-remote-runner.yml` and the sandbox image.

Two of the originals are genuinely gone rather than ported: they asserted on
the gateway's own `backend` service, which this repository does not have.
`test_the_dropped_assertions_stay_dropped_deliberately` records which, so their
absence reads as a decision rather than an oversight.

The Kata preflight — the fourth property in the same set — lives in
`test_setup_kata_runtime.py`, which drives the real script.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import tomllib
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


def test_the_sandbox_network_is_internal(compose: dict) -> None:
    """The agent-phase tier expressed as infrastructure.

    A sandbox on an internal network reaches what is on that network and
    nothing else, whatever the agent inside decides to try. Here the only
    thing on it is the tunnel, and the tunnel goes one place.
    """
    assert compose["networks"]["agent-egress"]["internal"] is True


def test_the_tunnel_is_the_only_thing_the_sandbox_network_carries(compose: dict) -> None:
    """An internal network is only a boundary if nothing else is attached.

    Adding a service to `agent-egress` is how the one-destination guarantee
    would be lost quietly — the network stays `internal: true` and the test
    above keeps passing.
    """
    on_egress = {
        name
        for name, svc in compose["services"].items()
        if "agent-egress" in (svc.get("networks") or {})
    }
    assert on_egress == {"agent-gateway-tunnel"}, (
        f"only the tunnel may sit on the sandbox network; found {on_egress}"
    )


# ---------------------------------------------------------------------------
# The runner's privileges
# ---------------------------------------------------------------------------


def test_the_runner_mounts_the_docker_socket_read_only(compose: dict) -> None:
    """The runner starts sandboxes; it has no business reconfiguring the daemon."""
    socket_mounts = [m for m in _mounts(compose["services"]["agent-runner"]) if "docker.sock" in m]
    assert socket_mounts, "the runner needs the daemon to spawn sandboxes"
    assert all(m.endswith(":ro") for m in socket_mounts), socket_mounts


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


def test_the_default_backend_isolates_by_kernel(compose: dict) -> None:
    """Defaulting to kata means a job gets its own kernel, not a shared one."""
    assert "kata" in compose["services"]["agent-runner"]["environment"]["AGENT_SANDBOX_BACKEND"]


def test_the_dispatcher_credential_is_required_not_defaulted(compose: dict) -> None:
    """Starting a runner without a dispatcher credential must fail loudly."""
    assert ":?" in compose["services"]["agent-runner"]["environment"]["AGENT_DISPATCHER_TOKEN"]


def test_the_sandbox_image_is_required_not_defaulted(compose: dict) -> None:
    """A defaulted image name is how a host silently runs something else."""
    assert ":?" in compose["services"]["agent-runner"]["environment"]["AGENT_SANDBOX_IMAGE"]


# ---------------------------------------------------------------------------
# The sandbox image
# ---------------------------------------------------------------------------


def test_the_sandbox_image_carries_no_credentials() -> None:
    """No secret may be baked into the image the agent runs in."""
    text = SANDBOX_DOCKERFILE.read_text(encoding="utf-8")
    for forbidden in ("GITHUB_TOKEN", "ANTHROPIC_API_KEY", "AGENT_DISPATCHER_TOKEN", "hyi-"):
        assert forbidden not in text, f"{forbidden} must never be baked into the sandbox image"


def test_the_sandbox_image_runs_as_non_root() -> None:
    """A kernel escape should have to start from an unprivileged user."""
    text = SANDBOX_DOCKERFILE.read_text(encoding="utf-8")
    assert "USER agent" in text
    assert text.index("USER agent") < text.index("WORKDIR /workspace")


def test_the_sandbox_uid_matches_the_one_the_runner_chowns_to() -> None:
    """A drift here is a job that cannot write a file in its own worktree.

    The runner chowns each worktree to this id before mounting it; if the two
    disagree, git refuses with "dubious ownership" — a failure that looks
    nothing like its cause, which is why both ends carry a comment pointing at
    the other.
    """
    dockerfile_uid = "--uid 10001"
    assert dockerfile_uid in SANDBOX_DOCKERFILE.read_text(encoding="utf-8")
    sandbox_py = (REPO / "host/cloud_agent_host/sandbox.py").read_text(encoding="utf-8")
    assert "SANDBOX_UID = 10001" in sandbox_py or "10001" in sandbox_py


def test_the_sandbox_image_disables_agent_phone_home() -> None:
    """Telemetry off, so a blocked request is not a mystery timeout mid-job."""
    text = SANDBOX_DOCKERFILE.read_text(encoding="utf-8")
    for flag in (
        "DISABLE_TELEMETRY",
        "DISABLE_AUTOUPDATER",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    ):
        assert flag in text


def test_codex_mcp_is_constrained_to_an_empty_requirements_allowlist() -> None:
    """User, project and plugin MCP servers are disabled after config merge.

    Codex treats top-level ``mcp_servers`` requirements as the allowlist, and
    applies the same empty allowlist again to plugin-provided servers — which
    is stronger than ``--ignore-user-config`` alone.
    """
    assert tomllib.loads(CODEX_REQUIREMENTS.read_text(encoding="utf-8")) == {"mcp_servers": {}}
    assert "codex-requirements.toml /etc/codex/requirements.toml" in SANDBOX_DOCKERFILE.read_text(
        encoding="utf-8"
    )
    assert "codex-requirements.toml /etc/codex/requirements.toml" in WORKFLOW.read_text(
        encoding="utf-8"
    )


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
