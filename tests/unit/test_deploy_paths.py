"""The deploy files must reference paths that exist in *this* repository.

D7 is the one migration task whose diff is mostly path edits rather than
imports, and nothing else checks them: a Dockerfile that `COPY`s a path from
the source repo's layout fails at build time on a runner host, long after
review. These tests read the files and assert every path they name is real.

They deliberately do not re-test *behaviour* the source repo already covers
(egress network internal, socket read-only, non-root sandbox). Those
assertions live in `test_agent_deploy_config.py`, which is still in the source
repo because it asserts on the co-located overlay compose — see MIGRATION.md's
open-decisions register.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
HOST = REPO / "deploy/host"
SANDBOX = REPO / "deploy/sandbox"

DEPLOY_FILES = sorted(
    p for p in (*HOST.iterdir(), *SANDBOX.iterdir()) if p.is_file() and p.name != ".gitkeep"
)

# `COPY <src> <dst>` / `COPY --from=x <src> <dst>` — src is repo-relative.
_COPY = re.compile(r"^COPY(?:\s+--\S+)*\s+(\S+)\s+\S+\s*$", re.MULTILINE)
# Paths this repo owns, wherever they appear in a shell script or compose file.
# Trailing dots are excluded: these paths are frequently named mid-prose in
# comments, and a sentence-ending period is not part of the filename.
_REPO_PATH = re.compile(r"(?:deploy|host|backend|tests)/[A-Za-z0-9._/-]*[A-Za-z0-9_/-]")


@pytest.mark.parametrize("path", DEPLOY_FILES, ids=lambda p: str(p.relative_to(REPO)))
def test_no_deploy_file_references_the_source_repo_layout(path: Path) -> None:
    """No `apps/backend`, `serving.`, `ops/`, or `deploy/docker` survives."""
    text = path.read_text(encoding="utf-8")
    stale = [
        marker
        for marker in ("apps/backend", "serving.agent_jobs", "deploy/docker/", "ops/deploy/")
        if marker in text
    ]
    assert not stale, f"{path.relative_to(REPO)} still points at the source repo: {stale}"


@pytest.mark.parametrize(
    "dockerfile",
    sorted(p for p in DEPLOY_FILES if p.name.startswith("Dockerfile")),
    ids=lambda p: p.name,
)
def test_every_dockerfile_copy_source_exists(dockerfile: Path) -> None:
    """A COPY of a path that moved is a build failure on a runner host."""
    missing = [
        src
        for src in _COPY.findall(dockerfile.read_text(encoding="utf-8"))
        # Build-stage copies (`--from=builder /app/...`) are absolute and not ours.
        if not src.startswith("/") and not (REPO / src).exists()
    ]
    assert not missing, f"{dockerfile.name} copies paths that do not exist: {missing}"


def test_the_runner_image_runs_the_host_package() -> None:
    text = (HOST / "Dockerfile.agent-runner").read_text(encoding="utf-8")
    assert "COPY host/cloud_agent_host/ cloud_agent_host/" in text
    assert '"cloud_agent_host.runner"' in text


def test_the_remote_compose_builds_from_files_that_exist() -> None:
    """Every build context/dockerfile pair in the compose must resolve."""
    compose = yaml.safe_load((HOST / "docker-compose.agent-remote-runner.yml").read_text())
    checked = 0
    for name, service in compose["services"].items():
        build = service.get("build")
        if not isinstance(build, dict):
            continue
        # Contexts are relative to the compose file's own directory.
        resolved = (HOST / build["context"] / build["dockerfile"]).resolve()
        assert resolved.exists(), f"service {name}: {build['dockerfile']} does not exist"
        checked += 1
    assert checked, "compose declares no builds — the path rewrite was not exercised"


def test_the_remote_compose_invokes_host_modules() -> None:
    """The commands must name the moved package, not the gateway's."""
    text = (HOST / "docker-compose.agent-remote-runner.yml").read_text(encoding="utf-8")
    assert "cloud_agent_host.workspace_broker" in text
    assert "cloud_agent_host.runner" in text


def test_shell_scripts_reference_real_paths() -> None:
    """Scripts name compose files and helpers; each must be in the tree."""
    findings: list[str] = []
    for script in sorted(HOST.glob("*.sh")):
        for candidate in set(_REPO_PATH.findall(script.read_text(encoding="utf-8"))):
            if not (REPO / candidate).exists():
                findings.append(f"{script.name} -> {candidate}")
    assert not findings, f"scripts point at paths that do not exist: {findings}"


def test_the_workflow_targets_this_repository() -> None:
    """A repository guard left pointing at the source repo never runs here."""
    text = (REPO / ".github/workflows/agent-job-runner.yml").read_text(encoding="utf-8")
    assert "HarvardMadSys/freeinference-cloud-agent" in text
    assert "HarvardMadSys/hybridInference" not in text
    assert "PYTHONPATH=host python -m cloud_agent_host.runner" in text
