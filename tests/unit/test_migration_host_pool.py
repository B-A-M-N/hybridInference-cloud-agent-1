"""The phase-G migrations, checked without a database.

The `dbtest` tier applies these for real, which is much stronger — but it is
opt-in, so a migration that does not parse, or one whose ``down_revision``
points at nothing, would sail through `make check` and fail at deploy. These
are the checks worth paying for in the default tier.

The chain check earns its place for a specific reason: several extraction tasks
add revisions to this directory at the same time, and two revisions claiming the
same parent is a branch Alembic refuses to upgrade — reported as "multiple
heads" long after the pull requests that caused it were reviewed separately.
"""

from __future__ import annotations

import ast
import contextlib
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
VERSIONS = REPO / "migrations/versions"
HOST_POOL = VERSIONS / "0002_host_pool.py"
ATTEMPT_HOST = VERSIONS / "0003_attempt_host.py"


def _literal_constants(path: Path) -> dict[str, object]:
    """Read a migration's top-level literal constants without importing it.

    Importing pulls in ``alembic.op``, which is only bound inside a migration
    context. Non-literal assignments (a tuple built by a join, say) are skipped
    rather than guessed at.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    values: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            with contextlib.suppress(ValueError):
                values[node.targets[0].id] = ast.literal_eval(node.value)
    return values


@pytest.mark.parametrize("path", [HOST_POOL, ATTEMPT_HOST])
def test_the_migration_parses(path: Path) -> None:
    """A migration that does not import is found at deploy time, not review."""
    ast.parse(path.read_text(encoding="utf-8"))


def test_the_revision_chain_is_linear() -> None:
    """One base, one head, and no revision claimed as parent twice.

    Two revisions sharing a ``down_revision`` is a branch, and Alembic refuses
    to ``upgrade head`` across one. It is easy to create by accident when
    separate tasks add migrations in parallel, and the error surfaces on the
    machine running the deploy rather than in either review.
    """
    revisions: dict[str, str | None] = {}
    for path in sorted(VERSIONS.glob("*.py")):
        constants = _literal_constants(path)
        revision = constants.get("revision")
        assert isinstance(revision, str), f"{path.name} declares no revision id"
        parent = constants.get("down_revision")
        assert parent is None or isinstance(parent, str), f"{path.name} has an odd down_revision"
        revisions[revision] = parent

    parents = [parent for parent in revisions.values() if parent is not None]
    assert len(parents) == len(set(parents)), f"a revision is claimed twice as parent: {parents}"
    for parent in parents:
        assert parent in revisions, f"down_revision {parent!r} names no revision in this directory"

    roots = [rev for rev, parent in revisions.items() if parent is None]
    heads = [rev for rev in revisions if rev not in set(parents)]
    assert len(roots) == 1, f"expected one base revision, found {roots}"
    assert len(heads) == 1, f"expected one head, found {heads}"


def test_the_host_pool_migration_is_reversible() -> None:
    """Every column it adds is named again in the downgrade.

    A downgrade that misses a column leaves the next upgrade's
    ``ADD COLUMN IF NOT EXISTS`` a silent no-op — the schema keeps a column
    from a version that was supposedly rolled back, and the constraint that
    went with it is gone.
    """
    source = HOST_POOL.read_text(encoding="utf-8")
    upgrade, downgrade = source.split("def downgrade()")

    added = set(re.findall(r"ADD COLUMN IF NOT EXISTS (\w+)", upgrade))
    dropped = set(re.findall(r'^        "(\w+)",$', downgrade, re.MULTILINE))

    assert added, "the upgrade adds no columns"
    assert added <= dropped, f"never dropped: {sorted(added - dropped)}"


def test_the_slot_counter_carries_a_non_negative_constraint() -> None:
    """The invariant belongs in the schema, not only in the store.

    ``slots_used`` is the one number a concurrency bug would corrupt silently:
    a host whose count drifted negative looks permanently over-committed and
    quietly stops claiming, with nothing in the logs. The CHECK turns that into
    a loud failure at the write that caused it.
    """
    source = HOST_POOL.read_text(encoding="utf-8")

    assert "CHECK (slots_used >= 0)" in source


def test_the_attempt_host_column_is_nullable_and_clears_on_delete() -> None:
    """Both properties are load-bearing, and both are one clause in the DDL.

    Nullable, because an attempt claimed by a runner that reports no host is
    the pre-pool shape and must keep working. ``ON DELETE SET NULL``, because
    #1158's "forget this host" would otherwise start failing on any machine
    that had ever run anything.
    """
    source = ATTEMPT_HOST.read_text(encoding="utf-8")
    upgrade = source.split("def downgrade()")[0]

    assert "ADD COLUMN IF NOT EXISTS host_id TEXT" in upgrade
    # The column declaration itself, not the partial index's predicate, which
    # legitimately says ``host_id IS NOT NULL``.
    assert not re.search(r"ADD COLUMN IF NOT EXISTS host_id TEXT NOT NULL", upgrade)
    assert "ON DELETE SET NULL" in upgrade
