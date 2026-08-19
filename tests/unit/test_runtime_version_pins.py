"""The runtime pins are configurable, complete, and provably exact.

Making the CLI versions build arguments moved them from source, where review
guaranteed their shape, to a deployment's `.env`, where nothing does. Three
things had to stay true across that move, and each fails silently:

* **Complete.** A runtime pinned in the Dockerfile but not passed through by
  `sandbox_host.sh` is unconfigurable — an operator sets it, redeploys, sees
  the log say nothing, and gets the old version. The reverse, a pass-through
  for an `ARG` that no longer exists, is configuration that quietly does
  nothing.
* **Defaulted.** The reviewed version stays in the Dockerfile beside the event
  parser written against it. An `ARG` without a default would make the image
  depend on a value only one caller supplies, and `docker build` on its own —
  which is what CI does — would produce an image with no CLI in it.
* **Verified.** Every pin is checked against the binary at build time. The
  labels are derived from these arguments, and the whole version-stamp
  pipeline (heartbeat → attempt record → checkpoint gate) treats a label as
  fact. An unverified pin lets `latest` produce an image whose label is a lie.

Read from the files rather than duplicated here on purpose: a list of the five
runtimes copied into this test would be a sixth place to forget.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO / "deploy/sandbox/Dockerfile.agent-sandbox"
SANDBOX_HOST = REPO / "deploy/host/sandbox_host.sh"

#: `ARG NAME_VERSION=1.2.3` — a pin, with the default that makes it optional.
_ARG_PIN = re.compile(r"^ARG ([A-Z0-9_]+_VERSION)=(\S+)$", re.MULTILINE)

#: `NAME="$(env_or_dotenv NAME)"` — the script's explicit configuration reads.
_SCRIPT_READ = re.compile(r"env_or_dotenv ([A-Z0-9_]+_VERSION)\)")

#: The `for name in A B C; do` list that becomes `--build-arg`.
_PASS_THROUGH = re.compile(r"for name in ((?:[A-Z0-9_]+_VERSION[ \t]*)+);")

#: `check claude "${CLAUDE_CODE_VERSION}"` at the end of the image build.
_VERIFIED = re.compile(r'check \S+ "\$\{([A-Z0-9_]+_VERSION)\}"')


def _pins() -> dict[str, str]:
    return dict(_ARG_PIN.findall(DOCKERFILE.read_text()))


def test_every_pin_has_a_reviewed_default() -> None:
    """`docker build` with no arguments must still produce the shipped image."""
    pins = _pins()
    assert pins, "no version pins found in the Dockerfile — regex drift?"
    for name, default in pins.items():
        assert default, f"{name} has an empty default"


def test_the_deploy_script_passes_through_exactly_the_pins() -> None:
    """A pin the deploy cannot set is a pin an operator will set and lose."""
    script = SANDBOX_HOST.read_text()
    read = set(_SCRIPT_READ.findall(script))
    match = _PASS_THROUGH.search(script)
    assert match, "sandbox_host.sh has no --build-arg pass-through loop — regex drift?"
    passed = set(match.group(1).split())

    assert read == set(_pins()), (
        "deploy/host/sandbox_host.sh reads "
        f"{sorted(read)} but the Dockerfile pins {sorted(_pins())}"
    )
    assert passed == read, (
        f"sandbox_host.sh reads {sorted(read)} but only passes {sorted(passed)} "
        "to docker build — the rest are read and dropped"
    )


def test_every_pin_is_verified_against_the_installed_binary() -> None:
    """Nothing else stops a dist-tag turning the version labels into fiction."""
    verified = set(_VERIFIED.findall(DOCKERFILE.read_text()))
    assert verified == set(_pins()), (
        f"the image build verifies {sorted(verified)} but pins {sorted(_pins())}; "
        "an unverified pin can be a range or a dist-tag, and its label would "
        "then describe a binary the image does not contain"
    )


def test_the_example_offers_every_pin() -> None:
    """An operator learns these exist by reading the example, or not at all."""
    example = (REPO / "deploy/host/.env.example").read_text()
    assigned = {
        match.group(1)
        for line in example.splitlines()
        if (match := re.match(r"\s*([A-Z0-9_]+_VERSION)=", line))
    }
    assert assigned == set(_pins()), (
        f"deploy/host/.env.example offers {sorted(assigned)}, pins are {sorted(_pins())}"
    )


# ── What the script actually runs ────────────────────────────────────────
#
# The checks above read files. These run `sandbox_host.sh up` against a fake
# docker and read back the argv it was handed, because "the name appears in
# the script" and "the build receives the argument" are different claims and
# only the second one is the feature.


_BASE_ENV = "\n".join(
    [
        "COMPOSE_PROJECT=freeinference-cloud-agent-sandbox-staging",
        "AGENT_DISPATCHER_TOKEN=t",
        "AGENT_SANDBOX_IMAGE=freeinference-cloud-agent-job:staging",
        "AGENT_TUNNEL_SSH_DESTINATION=user@gateway",
        "AGENT_TUNNEL_SSH_KEY=/dev/null",
        "AGENT_TUNNEL_SSH_KNOWN_HOSTS=/etc/hosts",
        # Skips the Kata preflight, which no developer machine passes. The
        # script warns loudly and continues, which is the documented behaviour
        # of this override and not something this test is asserting about.
        "AGENT_SANDBOX_BACKEND=container",
        "",
    ]
)


def _up(tmp_path: Path, dotenv: str) -> str:
    """Run `up` with a recording docker; return every argv it received.

    The fake docker fails the first `compose` call on purpose. Everything under
    test here — reading the pins, and handing them to `docker build` — happens
    before that, and stopping there skips the bring-up's five-second settle.
    """
    app_dir = tmp_path / "app"
    app_dir.mkdir(exist_ok=True)
    (app_dir / ".env").write_text(dotenv)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    docker = fake_bin / "docker"
    docker.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$DOCKER_LOG"\n[ "$1" = compose ] && exit 1\nexit 0\n'
    )
    docker.chmod(0o755)

    log = tmp_path / "docker.log"
    subprocess.run(
        ["bash", str(SANDBOX_HOST), "up"],
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "APP_DIR": str(app_dir),
            "DOCKER_LOG": str(log),
            # The caller's own environment must not stand in for the .env
            # under test — every pin is read through the same precedence.
            "COMPOSE_PROJECT": "",
            **dict.fromkeys(_pins(), ""),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return log.read_text() if log.exists() else ""


def _build_line(invocations: str) -> str:
    for line in invocations.splitlines():
        if line.startswith("build "):
            return line
    raise AssertionError(f"docker build was never invoked; got:\n{invocations}")


def test_no_override_builds_the_reviewed_image(tmp_path: Path) -> None:
    """An unset pin passes no argument, so the Dockerfile default stands.

    This is the shape every existing host is in, and it has to keep producing
    exactly the image it produced before the pins were configurable.
    """
    build = _build_line(_up(tmp_path, _BASE_ENV))

    assert "--build-arg" not in build, (
        f"an unconfigured host passed a build argument anyway: {build}"
    )


def test_a_pin_in_dotenv_reaches_docker_build(tmp_path: Path) -> None:
    """The whole feature: a version in `.env` is the version that gets built."""
    build = _build_line(_up(tmp_path, _BASE_ENV + "CLAUDE_CODE_VERSION=9.9.9\n"))

    assert "--build-arg CLAUDE_CODE_VERSION=9.9.9" in build
    # And only that one. A pin nobody set must not be passed as empty, which
    # would override the Dockerfile default with nothing and install `pkg@`.
    assert build.count("--build-arg") == 1, build
