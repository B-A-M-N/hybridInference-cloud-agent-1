"""Every environment variable the code reads is configurable and written down.

ENVIRONMENT.md ends with "the PR checklist enforces this; a new variable
without a row is a review stop". It did not: `AGENT_BROKER_URL_ALLOWED_HOSTS`
and `AGENT_CONTROL_PLANE_URL` were both added without rows, and the first was
worse than undocumented — it is the host allowlist that decides who receives
the pool's shared broker token, and it was never wired into the control-plane
compose file at all. The deployed stack could not turn it on. A checklist the
author ticks for themselves is not a control over the author.

Two invariants, and the second is the one that bites:

* **Documented.** A variable read anywhere must appear in ENVIRONMENT.md.
  Cheap, and it is how an operator learns the variable exists.
* **Reachable.** A variable the *control plane* reads must be a key under
  `environment:` in `deploy/control-plane/docker-compose.yml`. Otherwise the
  code's fallback is the only behaviour the deployment can ever have — and for
  a security control, the fallback is "off" while the code, the tests and the
  review all say the control exists.

Only the control plane gets the compose check. Host variables are set by an
operator on the machine, migration variables are set at a shell for one run and
deliberately belong in no unit file, and a check that demanded compose keys for
those would be demanding the wrong thing.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTROL_PLANE_COMPOSE = REPO_ROOT / "deploy" / "control-plane" / "docker-compose.yml"
INVENTORY = REPO_ROOT / "ENVIRONMENT.md"

#: The prefixes this project owns. A bare `PATH` or `HOME` read is not ours to
#: document, and listing every variable any dependency might read would make
#: the check noise that people learn to silence.
_VAR = re.compile(r'["\'](AGENT_[A-Z0-9_]+|GATEWAY_[A-Z0-9_]+|IDENTITY_[A-Z0-9_]+)["\']')

#: Read by the control plane, and correctly absent from its compose file.
#: Each entry needs a reason, because "add it to the exemption list" is the
#: obvious way to make this test stop failing without fixing anything.
_COMPOSE_EXEMPT = {
    # Named by `deploy/migrate/`, which a human runs once at a shell against a
    # database this service will never open again. ENVIRONMENT.md says these
    # belong in no unit file or image; a compose key would contradict it.
    "AGENT_MIGRATION_SOURCE_URL",
    "AGENT_MIGRATION_OLD_KEY_FILE",
    # Deliberately not offered by this compose file, which mounts the key as a
    # read-only file instead. The code still accepts an inline PEM for
    # deployments that do not use this stack; wiring it here would put a
    # private key in the container environment, where `docker inspect`, crash
    # dumps and most log shippers can reach it. ENVIRONMENT.md says so in the
    # row, so an operator does not set it and wonder why nothing happens.
    "AGENT_GITHUB_APP_PRIVATE_KEY",
}


def _sources(*globs: str) -> list[Path]:
    found: list[Path] = []
    for pattern in globs:
        found += [p for p in REPO_ROOT.glob(pattern) if "test" not in p.name]
    return found


def _vars_read(paths: list[Path]) -> set[str]:
    names: set[str] = set()
    for path in paths:
        names |= set(_VAR.findall(path.read_text()))
    return names


def _compose_environment_keys() -> set[str]:
    """Keys under `environment:` — not merely text present in the file.

    A variable mentioned only in a comment is not wired to anything, and the
    comments here are long enough that a substring search over the raw file
    would pass on the strength of the sentence explaining the variable.
    """
    document = yaml.safe_load(CONTROL_PLANE_COMPOSE.read_text())
    keys: set[str] = set()
    for service in (document.get("services") or {}).values():
        environment = service.get("environment") or {}
        keys |= set(environment if isinstance(environment, dict) else ())
    return keys


def test_control_plane_variables_are_wired_into_compose() -> None:
    """A variable the control plane reads can be set on the deployed stack."""
    read = _vars_read(_sources("backend/**/*.py")) - _COMPOSE_EXEMPT
    unreachable = sorted(read - _compose_environment_keys())
    assert not unreachable, (
        "the control plane reads these, but they are not keys under "
        f"`environment:` in {CONTROL_PLANE_COMPOSE.relative_to(REPO_ROOT)}, so a "
        f"deployment cannot set them: {unreachable}"
    )


def test_every_variable_read_has_an_inventory_row() -> None:
    """A variable an operator might need to set is written down."""
    read = _vars_read(
        _sources(
            "backend/**/*.py",
            "host/**/*.py",
            "migrations/**/*.py",
            "deploy/migrate/*.py",
        )
    )
    inventory = INVENTORY.read_text()
    # Backticked, which is how every row writes them — and how a grouped row
    # like ``AGENT_SANDBOX_UID`` / ``AGENT_SANDBOX_GID`` still matches both.
    undocumented = sorted(name for name in read if f"`{name}`" not in inventory)
    assert not undocumented, (
        f"add a row to {INVENTORY.name} for each of these, in the right section, "
        f"marked NEW: {undocumented}"
    )
