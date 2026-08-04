"""Scaffold smoke tests.

These assert the two packages are importable and independently versioned, and —
more usefully — that the control plane and host packages never import gateway
code. Rule 5 in AGENTS.md is easy to violate by pasting a moved file that still
references ``serving.*``; this catches it in CI rather than at deploy time.
"""

from __future__ import annotations

import ast
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


# The gateway's signing roots, by name. This service must never hold either:
# control tokens re-keyed to AGENT_CONTROL_TOKEN_SECRET in E3, and inference is
# authorized by grants the gateway mints and verifies.
_GATEWAY_SECRETS = ("API_KEY_SECRET", "JWT_SECRET_KEY")


def _executable_strings(tree: ast.AST) -> list[str]:
    """Every string literal that is not a docstring.

    Docstrings are excluded because they legitimately *explain* what this
    service no longer uses, and losing that explanation would cost more than
    the guard is worth. Comments never appear in the AST at all, which is the
    right outcome for the same reason: neither can be a use.
    """
    docstrings = {
        ast.get_docstring(node, clean=False)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value not in docstrings
    ]


def test_no_source_file_reads_a_gateway_signing_secret() -> None:
    """AGENTS.md rule 4, enforced rather than asserted in prose.

    A reference creeping back in — via a later move, a copied fixture — is
    exactly the coupling the split exists to remove.
    """
    offenders: list[str] = []
    for root in (*_PACKAGE_ROOTS, _REPO_ROOT / "tests"):
        for path in root.rglob("*.py"):
            if path.name == Path(__file__).name:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
                node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
            }
            literals = _executable_strings(tree)
            for secret in _GATEWAY_SECRETS:
                if secret in names or any(secret in text for text in literals):
                    offenders.append(f"{path.relative_to(_REPO_ROOT)} -> {secret}")
    assert not offenders, f"gateway signing secret used in {offenders}"
