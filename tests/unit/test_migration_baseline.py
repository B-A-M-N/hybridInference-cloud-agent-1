"""The baseline migration must reproduce the gateway's schema exactly.

E1's real risk is not that the migration fails to run — it is that it runs and
produces a schema that is *nearly* the source's. A column that ended up
``TEXT`` instead of ``TIMESTAMPTZ``, or an index quietly dropped as redundant,
surfaces at cutover against rows nobody can afford to lose.

So these tests compare the migration against the source, not against an idea
of what the schema should look like. The `dbtest` tier applies it for real; see
`tests/integration/storage/test_agent_job_store.py`.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BASELINE = REPO / "migrations/versions/0001_baseline.py"
STORE = REPO / "backend/cloud_agent/storage/store.py"

# The 11 tables the gateway's AgentJobStore created at the freeze commit.
# Written out rather than derived, so a table silently vanishing from the
# migration fails here instead of agreeing with itself.
EXPECTED_TABLES = {
    "agent_threads",
    "agent_jobs",
    "agent_attempts",
    "agent_job_events",
    "agent_thread_messages",
    "agent_job_artifacts",
    "agent_repo_grants",
    "agent_oauth_states",
    "agent_gitlab_connections",
    "agent_runner_hosts",
    "agent_runner_policy",
}


def _statements() -> tuple[str, ...]:
    """Return the baseline's DDL, read as data rather than executed."""
    tree = ast.parse(BASELINE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        target = None
        if isinstance(node, ast.AnnAssign):
            target = getattr(node.target, "id", None)
        elif isinstance(node, ast.Assign):
            target = getattr(node.targets[0], "id", None)
        if target == "_STATEMENTS":
            return tuple(ast.literal_eval(node.value))
    raise AssertionError("_STATEMENTS not found in the baseline migration")


def test_the_baseline_creates_every_table_the_gateway_did() -> None:
    created = {
        match.group(1)
        for statement in _statements()
        for match in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+)", statement)
    }
    assert created == EXPECTED_TABLES


def test_the_baseline_keeps_the_add_column_trail() -> None:
    """Those ALTERs are not redundant — they are the source's migration history.

    ``CREATE TABLE IF NOT EXISTS`` does nothing when the table exists with the
    *wrong* columns, which is why every later schema change in the source was
    re-stated as an ALTER. Dropping them here would produce a schema that is
    correct on an empty database and wrong on an existing one.
    """
    alters = [s for s in _statements() if "ADD COLUMN IF NOT EXISTS" in s]
    assert len(alters) >= 10, f"only {len(alters)} ALTERs survived; the trail was trimmed"


def test_the_baseline_creates_indexes() -> None:
    indexes = {
        match.group(1)
        for statement in _statements()
        for match in re.finditer(r"CREATE (?:UNIQUE )?INDEX IF NOT EXISTS (\w+)", statement)
    }
    assert len(indexes) >= 8, f"only {len(indexes)} indexes: {sorted(indexes)}"


def test_no_statement_was_normalised_into_a_different_type() -> None:
    """Spot-check the types most likely to be "tidied" into something wrong."""
    joined = "\n".join(_statements())
    # Timestamps must stay timezone-aware: a naive column silently shifts every
    # lease expiry and reaper decision by the server's offset.
    assert "TIMESTAMPTZ" in joined
    assert not re.search(r"\bTIMESTAMP\b(?! WITH)(?!TZ)", joined)
    # JSONB, not JSON: the source queries these with containment operators.
    assert "JSONB" in joined


def test_the_downgrade_drops_every_table_it_creates() -> None:
    """A downgrade that misses a table leaves the next upgrade a no-op."""
    dropped = set(re.findall(r"DROP TABLE IF EXISTS \{table\}", BASELINE.read_text()))
    listed = set(re.findall(r'^        "(agent_\w+)",$', BASELINE.read_text(), re.MULTILINE))
    assert dropped, "downgrade does not drop anything"
    assert listed == EXPECTED_TABLES


# ---------------------------------------------------------------------------
# The store must no longer own the schema
# ---------------------------------------------------------------------------


def test_the_store_no_longer_creates_tables_at_startup() -> None:
    """Schema is Alembic's now.

    Leaving startup DDL in place would mean two sources of truth, and the one
    that runs first wins silently.

    Checked against the parsed source rather than the raw text: the store's own
    docstrings *discuss* ``CREATE TABLE IF NOT EXISTS`` — explaining why it was
    not enough — and a substring search cannot tell an explanation from an
    execution.
    """
    tree = ast.parse(STORE.read_text(encoding="utf-8"))
    # clean=False: the default dedents, and the raw constants below would then
    # never match their own docstrings.
    docstrings = {
        ast.get_docstring(node, clean=False)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    offenders = [
        node.value[:60]
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value not in docstrings
        and ("CREATE TABLE" in node.value or "ADD COLUMN" in node.value)
    ]
    assert not offenders, f"the store still executes DDL: {offenders}"


def test_initialize_survives_as_a_no_op() -> None:
    """Kept so E6's bootstrap call site moves unchanged."""
    tree = ast.parse(STORE.read_text(encoding="utf-8"))
    initialize = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "initialize"
    ]
    assert len(initialize) == 1, "initialize() should still exist, exactly once"
    # Docstring plus a bare return: nothing that touches a connection.
    body = initialize[0].body
    assert all(isinstance(node, (ast.Expr, ast.Return)) for node in body)


def test_the_store_imports_no_gateway_module() -> None:
    source = STORE.read_text(encoding="utf-8")
    assert "serving." not in source


@pytest.mark.parametrize("path", [BASELINE, REPO / "migrations/env.py"])
def test_migration_files_parse(path: Path) -> None:
    """A migration that does not import is found at deploy time, not review."""
    ast.parse(path.read_text(encoding="utf-8"))


def test_the_baseline_keeps_the_startup_backfills() -> None:
    """`initialize` was not only DDL, and the migration must carry the rest.

    Five statements backfilled data on every boot: threads and thread messages
    for jobs that predate them, the singleton runner-policy row, and
    thread_id/turn_no on legacy jobs. On a fresh database they are no-ops; on
    the database H1 imports rows into they decide whether the thread view has
    anything in it.

    They are easy to mistake for leftovers because they hid inside a method
    named for schema. This test is what makes deleting them fail.
    """
    verbs = [statement.strip().split(None, 1)[0].upper() for statement in _statements()]
    assert verbs.count("INSERT") == 4, "a thread/message/policy backfill was dropped"
    assert verbs.count("UPDATE") == 1, "the legacy-job thread_id backfill was dropped"


def test_the_store_docstring_does_not_claim_the_move_was_ddl_only() -> None:
    """Guards the wording, because the wording is what a future reader trusts.

    An `initialize` documented as "created the tables" invites someone to
    treat the migration's INSERTs as noise.
    """
    tree = ast.parse(STORE.read_text(encoding="utf-8"))
    initialize = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "initialize"
    )
    doc = ast.get_docstring(initialize) or ""
    assert "backfill" in doc.lower()
