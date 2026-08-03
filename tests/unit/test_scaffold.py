"""Scaffold smoke tests.

These assert the two packages are importable and independently versioned, and —
more usefully — that the control plane and host packages never import gateway
code. Rule 5 in AGENTS.md is easy to violate by pasting a moved file that still
references ``serving.*``; this catches it in CI rather than at deploy time.
"""

from __future__ import annotations

import re
from pathlib import Path

import cloud_agent
import cloud_agent_host

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE_ROOTS = (_REPO_ROOT / "backend", _REPO_ROOT / "host")

# Matches `import serving...` and `from serving... import ...`, which would mean
# a moved file still reaches into the HybridInference gateway package.
_GATEWAY_IMPORT = re.compile(r"^\s*(?:from|import)\s+serving\b", re.MULTILINE)


def test_packages_are_importable() -> None:
    assert cloud_agent.__version__
    assert cloud_agent_host.__version__


def test_no_gateway_imports() -> None:
    offenders = [
        path.relative_to(_REPO_ROOT)
        for root in _PACKAGE_ROOTS
        for path in root.rglob("*.py")
        if _GATEWAY_IMPORT.search(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        f"gateway imports found in {offenders}; the gateway is reachable over HTTP only "
        "(AGENTS.md rule 5)"
    )
