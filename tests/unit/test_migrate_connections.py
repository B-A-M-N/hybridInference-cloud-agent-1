"""The connection migration's decisions, without a database.

The script's whole reason to exist is that E5 re-keyed the source-control
cipher, so the first test here is the one that would have caught a "just copy
the rows" implementation: ciphertext written under the gateway's key does not
decrypt under this service's, and the only reason a re-key is even possible is
that the domain separator did not change with it.

Everything else is the machinery around that step — deciding what to insert,
what already matches, and what collides — plus the two properties that make the
report trustworthy: the checksum sees plaintext (so it is comparable across a
re-encryption) and the old key never arrives through the environment.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet

from cloud_agent.source_control import SourceControlCipher

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "deploy/migrate/migrate_connections.py"


def _load_script() -> Any:
    """Import the script by path — `deploy/` is tooling, not an importable package."""
    spec = importlib.util.spec_from_file_location("migrate_connections", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: the module defines dataclasses under
    # `from __future__ import annotations`, and resolving those annotations
    # looks the module up in `sys.modules` by name.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mc = _load_script()

OLD_MATERIAL = "gateway-api-key-secret-material"
NEW_MATERIAL = "agent-source-control-key-material"

_NOW = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


def _gateway_cipher(material: str) -> Fernet:
    """The gateway's derivation, restated by hand.

    Restated rather than imported, so this test fails if the moved module
    changes the derivation. Copied from
    `764a6f97:apps/backend/serving/agent_jobs/source_control.py:39-47`, with the
    key material as the only difference.
    """
    digest = hashlib.sha256(
        b"hybridinference/source-control/v1\0" + material.encode("utf-8")
    ).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _connection(
    row_id: int,
    user_id: str,
    cipher: Any,
    *,
    access: str = "gitlab-access-token",
    refresh: str = "gitlab-refresh-token",
) -> dict[str, Any]:
    return {
        "id": row_id,
        "user_id": user_id,
        "external_user_id": 900 + row_id,
        "username": f"user{row_id}",
        "display_name": None,
        "web_url": "https://gitlab.com/user",
        "access_token_ciphertext": cipher.encrypt(access.encode("utf-8")).decode("ascii")
        if isinstance(cipher, Fernet)
        else cipher.encrypt(access),
        "refresh_token_ciphertext": cipher.encrypt(refresh.encode("utf-8")).decode("ascii")
        if isinstance(cipher, Fernet)
        else cipher.encrypt(refresh),
        "expires_at": _NOW + timedelta(hours=2),
        "created_at": _NOW,
        "updated_at": _NOW,
    }


# ── The re-key itself ──────────────────────────────────────────────────


def test_the_cipher_derivation_differs_only_in_key_material() -> None:
    """The transform is decrypt-with-old, encrypt-with-new — and nothing else.

    If the domain separator had changed too, no amount of key juggling would
    recover the rows, and this migration would be impossible rather than
    merely delicate.
    """
    hand_derived = _gateway_cipher(OLD_MATERIAL)
    token = hand_derived.encrypt(b"gitlab-access-token").decode("ascii")
    assert SourceControlCipher(OLD_MATERIAL).decrypt(token) == "gitlab-access-token"


def test_copying_the_ciphertext_would_produce_a_broken_connection() -> None:
    """The failure this script exists to prevent, pinned.

    A copied row looks present — right username, right expiry, listed in the
    UI — and dies at the first token read.
    """
    old = SourceControlCipher(OLD_MATERIAL)
    new = SourceControlCipher(NEW_MATERIAL)
    copied = old.encrypt("gitlab-access-token")
    with pytest.raises(Exception, match="could not be decrypted"):
        new.decrypt(copied)


def test_decryption_failure_aborts_the_whole_table() -> None:
    """One unreadable row stops the run; a partial import must not look like success."""
    old = SourceControlCipher(OLD_MATERIAL)
    good = _connection(1, "u1", old)
    bad = _connection(2, "u2", SourceControlCipher("a-third-key"))
    with pytest.raises(mc.MigrationError, match="did not decrypt"):
        mc.decrypt_rows(mc.GITLAB_CONNECTIONS, [good, bad], old)


def test_a_table_without_ciphertext_columns_needs_no_cipher() -> None:
    """`agent_repo_grants` carries no encrypted column — it is a plain copy."""
    rows = [{"id": 1, "user_id": "u1", "installation_id": 5, "account_login": "acme"}]
    assert mc.decrypt_rows(mc.REPO_GRANTS, rows, None) == [{}]
    assert mc.REPO_GRANTS.ciphertext_columns == ()


# ── The checksum ───────────────────────────────────────────────────────


def test_the_checksum_survives_re_encryption() -> None:
    """Two encryptions of the same plaintext must check out equal.

    Fernet embeds a random IV and a timestamp, so the stored bytes differ on
    every call. A checksum over ciphertext would report DIFFERS on a perfectly
    correct migration, which is worse than having no checksum at all.
    """
    old, new = SourceControlCipher(OLD_MATERIAL), SourceControlCipher(NEW_MATERIAL)
    source = _connection(1, "u1", old)
    target = dict(source)
    target["access_token_ciphertext"] = new.encrypt("gitlab-access-token")
    target["refresh_token_ciphertext"] = new.encrypt("gitlab-refresh-token")
    assert source["access_token_ciphertext"] != target["access_token_ciphertext"]

    spec = mc.GITLAB_CONNECTIONS
    source_sum = mc.checksum(
        spec, [mc.row_fingerprint(spec, source, mc.decrypt_rows(spec, [source], old)[0])]
    )
    target_sum = mc.checksum(
        spec, [mc.row_fingerprint(spec, target, mc.decrypt_rows(spec, [target], new)[0])]
    )
    assert source_sum == target_sum


def test_the_checksum_notices_a_changed_token() -> None:
    """A re-key that quietly dropped a token would still have to fail here."""
    old, new = SourceControlCipher(OLD_MATERIAL), SourceControlCipher(NEW_MATERIAL)
    source = _connection(1, "u1", old)
    target = dict(source)
    target["access_token_ciphertext"] = new.encrypt("a-different-token")
    target["refresh_token_ciphertext"] = new.encrypt("gitlab-refresh-token")

    spec = mc.GITLAB_CONNECTIONS
    source_sum = mc.checksum(
        spec, [mc.row_fingerprint(spec, source, mc.decrypt_rows(spec, [source], old)[0])]
    )
    target_sum = mc.checksum(
        spec, [mc.row_fingerprint(spec, target, mc.decrypt_rows(spec, [target], new)[0])]
    )
    assert source_sum != target_sum


def test_the_checksum_does_not_depend_on_row_order() -> None:
    """Two databases need not return rows in the same order."""
    spec = mc.REPO_GRANTS
    digests = [b"a" * 32, b"b" * 32, b"c" * 32]
    assert mc.checksum(spec, digests) == mc.checksum(spec, list(reversed(digests)))


def test_null_and_the_empty_string_do_not_fingerprint_alike() -> None:
    """`account_login` is nullable; NULL and '' are different rows."""
    spec = mc.REPO_GRANTS
    base = {"id": 1, "user_id": "u1", "installation_id": 5, "created_at": _NOW}
    null_row = mc.row_fingerprint(spec, {**base, "account_login": None}, {})
    empty_row = mc.row_fingerprint(spec, {**base, "account_login": ""}, {})
    assert null_row != empty_row


def test_an_empty_table_is_not_every_other_empty_table() -> None:
    """Domain separation by name, so two empty tables do not check out alike."""
    assert mc.checksum(mc.REPO_GRANTS, []) != mc.checksum(mc.GITLAB_CONNECTIONS, [])


# ── Planning ───────────────────────────────────────────────────────────


def _grant(row_id: int, user_id: str, installation_id: int, login: str = "acme") -> dict[str, Any]:
    return {
        "id": row_id,
        "user_id": user_id,
        "installation_id": installation_id,
        "account_login": login,
        "created_at": _NOW,
    }


def test_a_fresh_target_is_all_inserts() -> None:
    """The cutover-day shape."""
    spec = mc.REPO_GRANTS
    source = [_grant(1, "u1", 10), _grant(2, "u2", 11)]
    plan = mc.plan_table(spec, source, [{}, {}], [], [])
    assert [row["id"] for row in plan.insert] == [1, 2]
    assert plan.unchanged == [] and plan.refresh == [] and plan.conflict == []
    assert not plan.blocked


def test_a_second_run_is_a_no_op() -> None:
    """Idempotency, which is the acceptance criterion this script is judged on."""
    spec = mc.REPO_GRANTS
    source = [_grant(1, "u1", 10), _grant(2, "u2", 11)]
    plan = mc.plan_table(spec, source, [{}, {}], list(source), [{}, {}])
    assert plan.insert == [] and plan.refresh == []
    assert [row["id"] for row in plan.unchanged] == [1, 2]
    assert plan.source_checksum == plan.target_checksum


def test_a_row_that_changed_upstream_is_reported_not_skipped() -> None:
    """The rehearsal hazard: import, then the user reconnects on the old stack.

    `INSERT … ON CONFLICT DO NOTHING` would leave the stale row in place and
    report success.
    """
    spec = mc.REPO_GRANTS
    source = [_grant(1, "u1", 10, login="acme-renamed")]
    target = [_grant(1, "u1", 10, login="acme")]
    plan = mc.plan_table(spec, source, [{}], target, [{}])
    assert [row["id"] for row in plan.refresh] == [1]
    assert plan.insert == [] and plan.unchanged == []
    assert plan.source_checksum != plan.target_checksum


def test_the_same_connection_made_twice_is_a_conflict_not_a_crash() -> None:
    """The target already holds this unique key under another id.

    Inserting would raise a duplicate-key error partway through the batch. The
    plan names the two rows instead.
    """
    spec = mc.REPO_GRANTS
    source = [_grant(7, "u1", 10)]
    target = [_grant(99, "u1", 10)]
    plan = mc.plan_table(spec, source, [{}], target, [{}])
    assert plan.blocked
    assert plan.conflict[0]["id"] == 7
    assert plan.conflict[0]["_target_id"] == 99


def test_ids_are_preserved_by_construction() -> None:
    """`id` is a migrating column, so an insert carries the source's value.

    DR5 leaves history behind but keeps these two tables id-stable, which is
    what lets an operator match a row across the two databases by eye.
    """
    assert mc.REPO_GRANTS.columns[0] == "id"
    assert mc.GITLAB_CONNECTIONS.columns[0] == "id"


def test_only_two_tables_migrate() -> None:
    """Guards DR5 against a well-meaning addition."""
    assert [spec.name for spec in mc.TABLES] == [
        "agent_repo_grants",
        "agent_gitlab_connections",
    ]


def test_agent_oauth_states_is_not_migrated() -> None:
    """The third ciphertext column in the schema stays behind, deliberately.

    `agent_oauth_states.code_verifier_ciphertext` has a ten-minute TTL and is
    single-use: any row still valid at cutover belongs to a flow that is about
    to be interrupted anyway.
    """
    assert "agent_oauth_states" not in {spec.name for spec in mc.TABLES}


# ── Secret handling and plumbing ───────────────────────────────────────


def test_the_old_key_is_never_taken_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AGENTS.md rule 4, enforced by the code rather than by a comment.

    A gateway secret sitting in the environment must not be silently picked
    up — that is exactly how it ends up in an env file on the agent host.
    """
    # Named through the script's own constant rather than as a literal: this
    # file is inside the tree `test_scaffold.py` scans for gateway secret
    # names, and a test that asserts the rule must not read as a breach of it.
    monkeypatch.setenv(mc.GATEWAY_SECRET_ENV_NAMES[0], OLD_MATERIAL)
    monkeypatch.setenv(mc.ENV_SOURCE_URL, "postgresql://x/source")
    monkeypatch.setenv(mc.ENV_TARGET_URL, "postgresql://x/target")
    monkeypatch.delenv(mc.ENV_OLD_KEY_FILE, raising=False)

    assert mc.main([]) == mc.EXIT_CONFIG
    assert "--old-key-file" in capsys.readouterr().err


def test_the_key_material_is_not_a_command_line_value() -> None:
    """`ps` is readable by every process on the host, so only paths are accepted."""
    options = {action.dest for action in mc.build_parser()._actions}
    assert "old_key_file" in options and "new_key_file" in options
    assert "old_key" not in options and "new_key" not in options


def test_reading_the_same_database_twice_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """One DSN pasted into both variables would import a database into itself."""
    key_file = tmp_path / "key"
    key_file.write_text(OLD_MATERIAL)
    monkeypatch.setenv(mc.ENV_SOURCE_URL, "postgresql://x/same")
    monkeypatch.setenv(mc.ENV_TARGET_URL, "postgresql://x/same")

    assert mc.main(["--old-key-file", str(key_file)]) == mc.EXIT_CONFIG
    assert "same database" in capsys.readouterr().err


def test_one_trailing_newline_is_stripped_from_key_material(tmp_path: Path) -> None:
    """Every editor adds one, and a key off by one byte decrypts nothing."""
    path = tmp_path / "key"
    path.write_text(OLD_MATERIAL + "\n")
    assert mc.read_key_material(str(path)) == OLD_MATERIAL


def test_empty_key_material_is_refused(tmp_path: Path) -> None:
    """An empty file must not derive a key from the empty string."""
    path = tmp_path / "key"
    path.write_text("\n")
    with pytest.raises(mc.MigrationError, match="empty"):
        mc.read_key_material(str(path))


def test_the_alembic_url_names_the_driver_this_project_installs() -> None:
    """`AGENT_DATABASE_URL` is an asyncpg DSN; SQLAlchemy would read psycopg2.

    This project depends on psycopg 3 and not on psycopg2, so left alone the
    upgrade fails on a missing module rather than on anything about the
    database.
    """
    assert mc.sqlalchemy_url("postgresql://u:p@h:5432/db") == "postgresql+psycopg://u:p@h:5432/db"
    assert mc.sqlalchemy_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    # Already explicit: left exactly as the operator wrote it.
    assert mc.sqlalchemy_url("postgresql+psycopg://u@h/db") == "postgresql+psycopg://u@h/db"


def test_the_report_names_the_counts_the_runbook_ticks() -> None:
    """docs/cutover.md tells the operator to read these; they must be printed."""
    plan = mc.TablePlan(spec=mc.REPO_GRANTS)
    plan.insert = [_grant(1, "u1", 10)]
    plan.source_checksum = plan.target_checksum = "abc"
    report = mc.render_report([plan], applied=True, revision="0001_baseline")
    for expected in ("agent_repo_grants", "checksum", "MATCH", "0001_baseline", "insert"):
        assert expected in report


def test_a_dry_run_says_so() -> None:
    """The default mode must be unmistakable in the transcript pasted into the ticket."""
    plan = mc.TablePlan(spec=mc.REPO_GRANTS)
    plan.source_checksum = plan.target_checksum = "abc"
    assert "nothing was written" in mc.render_report(
        [plan], applied=False, revision="0001_baseline"
    )


def test_a_refused_apply_admits_the_schema_was_written() -> None:
    """`--apply` upgrades before it can refuse, so "nothing was written" is false.

    The tables have to exist before the target can be read, so `alembic upgrade
    head` runs first and a conflict found afterwards leaves a target whose
    schema has moved. An operator told nothing was written does not think to
    check it.
    """
    plan = mc.TablePlan(spec=mc.REPO_GRANTS)
    plan.source_checksum, plan.target_checksum = "abc", "def"

    report = mc.render_report(
        [plan], applied=False, revision="0001_baseline", refused=True, schema_upgraded=True
    )

    assert "no rows were written" in report
    assert "schema WAS written" in report
    assert "0001_baseline" in report
    assert "nothing was written" not in report


def test_a_refusal_that_upgraded_nothing_does_not_claim_it_did() -> None:
    """`--skip-migrations`, or a refusal before the upgrade: no schema note."""
    plan = mc.TablePlan(spec=mc.REPO_GRANTS)
    plan.source_checksum = plan.target_checksum = "abc"

    report = mc.render_report(
        [plan], applied=False, revision="0001_baseline", refused=True, schema_upgraded=False
    )

    assert "no rows were written" in report
    assert "schema WAS written" not in report


def test_the_target_column_is_the_targets_row_count() -> None:
    """Not "source rows already present", which is a different number.

    The checksum below it is taken over every row in the target, so a target
    holding a row the source does not — a connection made on the new stack
    during a rehearsal — reads as DIFFERS against a `target` column that had
    not counted it. Two numbers describing different sets, one above the other.
    """
    spec = mc.REPO_GRANTS
    source = [_grant(1, "u1", 10)]
    target = [_grant(1, "u1", 10), _grant(2, "u2", 11)]

    plan = mc.plan_table(spec, source, [{}], target, [{}, {}])

    assert plan.target_total == 2
    assert len(plan.unchanged) == 1
    row = next(
        line
        for line in mc.render_report([plan], applied=False, revision="0001_baseline").splitlines()
        if line.startswith(spec.name)
    )
    assert row.split() == [spec.name, "1", "2", "0", "0", "1", "0"]


def test_the_ignored_gateway_secret_is_announced_before_anything_can_refuse(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The notice has to reach the operator who most needs it.

    That operator set the gateway's secret in the environment believing this
    script would use it, and therefore did *not* pass `--old-key-file` — so
    printing the notice after the configuration checks meant it was unreachable
    in exactly the case it was written for.
    """
    monkeypatch.setenv(mc.GATEWAY_SECRET_ENV_NAMES[0], OLD_MATERIAL)
    monkeypatch.delenv(mc.ENV_SOURCE_URL, raising=False)
    monkeypatch.delenv(mc.ENV_TARGET_URL, raising=False)
    monkeypatch.delenv(mc.ENV_OLD_KEY_FILE, raising=False)

    assert mc.main([]) == mc.EXIT_CONFIG

    stderr = capsys.readouterr().err
    notice, *_ = stderr.splitlines()
    assert mc.GATEWAY_SECRET_ENV_NAMES[0] in notice
    assert "is being ignored" in notice
    # First line, ahead of the configuration error that ended the run — which
    # is where the notice used to be unreachable.
    assert "must both be set" in stderr


def test_the_same_id_under_a_different_owner_is_a_conflict_not_a_refresh() -> None:
    """**The one that would have handed a user somebody else's repositories.**

    Both databases assign ids of their own, so a numeric collision between them
    is ordinary rather than a sign of anything. The plan matched on `id` alone,
    so a source row and an unrelated target row that happened to share a number
    were read as the same connection needing a refresh — and the import would
    overwrite one user's source-control credential with another's.

    Silently, and with the migration reporting success: the row counts and the
    checksums would all agree, because from the plan's point of view exactly
    one row was updated.
    """
    spec = mc.REPO_GRANTS
    source = [_grant(5, "u1", 10)]
    target = [_grant(5, "u2", 20)]

    plan = mc.plan_table(spec, source, [{}], target, [{}])

    assert plan.blocked, "an id collision across owners was planned as an update"
    assert plan.refresh == [] and plan.unchanged == []
    assert plan.conflict[0]["id"] == 5
    assert plan.conflict[0]["_target_id"] == 5


def test_the_same_id_under_the_same_owner_still_refreshes() -> None:
    """The other half: an id that names the same connection is exactly what a
    re-run is for, and refusing it would make the import non-idempotent."""
    spec = mc.REPO_GRANTS
    source = [_grant(5, "u1", 10, login="acme-renamed")]
    target = [_grant(5, "u1", 10, login="acme")]

    plan = mc.plan_table(spec, source, [{}], target, [{}])

    assert [row["id"] for row in plan.refresh] == [5]
    assert not plan.blocked
