"""Choosing a CLI version: what gets picked, and what actually runs.

Three seams, each of which fails silently if it is wrong, which is why each is
pinned here rather than left to the end-to-end run:

* **Resolution order** decides which version an attempt is stamped with. Get it
  wrong in favour of the admin's selection and every in-flight thread loses its
  session the moment somebody switches — the checkpoint gate refuses a
  mismatched version by downgrading to replay, and says so only in a log line.
* **Label parsing** decides what the pool believes each host can run. Degrade a
  missing menu to *empty* rather than to "just the default" and every stored
  selection becomes unservable at once.
* **PATH selection** decides what the sandbox executes. This is the one that
  cannot be allowed to fall back: the binary name still resolves without the
  versioned directory — to the image default — so a job would run one version
  while its attempt row, the audit trail and the checkpoint gate all record
  another.
"""

from __future__ import annotations

from typing import Any

import pytest

from cloud_agent.storage.store import resolve_runtime_version
from cloud_agent_host.runner import (
    ClaimedJob,
    _select_runtime_version,
    runtime_version_unavailable,
)
from cloud_agent_host.sandbox import (
    SANDBOX_PATH,
    SandboxError,
    _labelled,
    _split_menu,
    runtime_bin_dir,
)

_MENU = {"claude-code": ["2.1.220", "2.1.235"]}
_DEFAULT = {"claude-code": "2.1.220"}


# ── Which version the claim stamps ───────────────────────────────────────


def test_the_host_default_applies_when_nothing_is_selected() -> None:
    """The behaviour every deployment has until an admin chooses."""
    assert (
        resolve_runtime_version(
            runtime="claude-code",
            thread_version=None,
            selected={},
            available=_MENU,
            host_default=_DEFAULT,
        )
        == "2.1.220"
    )


def test_a_selection_the_host_offers_wins_over_its_default() -> None:
    """The feature."""
    assert (
        resolve_runtime_version(
            runtime="claude-code",
            thread_version=None,
            selected={"claude-code": "2.1.235"},
            available=_MENU,
            host_default=_DEFAULT,
        )
        == "2.1.235"
    )


def test_a_thread_keeps_the_version_it_started_on() -> None:
    """An admin switching versions must not reach into live conversations.

    ``gate_for_claim`` refuses a checkpoint whose exported driver version
    differs from the attempt's stamp — silently, into replay. Honouring the new
    selection here would make one admin click cost every running thread its
    memory, with nothing on screen to say why.
    """
    assert (
        resolve_runtime_version(
            runtime="claude-code",
            thread_version="2.1.220",
            selected={"claude-code": "2.1.235"},
            available=_MENU,
            host_default=_DEFAULT,
        )
        == "2.1.220"
    )


def test_a_selection_this_host_cannot_serve_falls_back() -> None:
    """An image rebuilt with a shorter menu must not park the queue.

    The selection outliving the version is a real sequence — rebuild the image
    without an extra, and the stored choice names something no longer installed.
    Running the default is a smaller wrong than refusing every claim.
    """
    assert (
        resolve_runtime_version(
            runtime="claude-code",
            thread_version=None,
            selected={"claude-code": "9.9.9"},
            available=_MENU,
            host_default=_DEFAULT,
        )
        == "2.1.220"
    )


def test_a_runtime_nobody_reported_stamps_unknown() -> None:
    """ "Unknown" is a truthful record; an invented version is not."""
    assert (
        resolve_runtime_version(
            runtime="codex",
            thread_version=None,
            selected={},
            available=_MENU,
            host_default=_DEFAULT,
        )
        is None
    )


def test_a_selection_for_another_runtime_is_not_applied() -> None:
    """Selections are per runtime; a codex choice says nothing about claude."""
    assert (
        resolve_runtime_version(
            runtime="claude-code",
            thread_version=None,
            selected={"codex": "0.145.0"},
            available=_MENU,
            host_default=_DEFAULT,
        )
        == "2.1.220"
    )


# ── What the pool believes each host holds ───────────────────────────────


def test_the_menu_and_the_default_come_from_different_labels() -> None:
    """``.version`` is not a suffix of ``.versions``, so the two never mix."""
    labels = {
        "org.freeinference.runtime.claude-code.version": "2.1.220",
        "org.freeinference.runtime.claude-code.versions": "2.1.220,2.1.235",
    }
    assert _labelled(labels, ".version") == {"claude-code": "2.1.220"}
    assert _labelled(labels, ".versions") == {"claude-code": "2.1.220,2.1.235"}


def test_an_image_with_no_menu_label_offers_its_default() -> None:
    """Built before menus existed — it holds one version, and offers it.

    Degrading to an empty list instead would read as "this host can run
    nothing" and make every stored selection unservable at once.
    """
    assert _split_menu(None, "2.1.220") == ["2.1.220"]
    assert _split_menu("", "2.1.220") == ["2.1.220"]


def test_a_menu_missing_its_own_default_still_offers_it() -> None:
    """The default is what an unselected job runs; it is always available."""
    assert _split_menu("2.1.235", "2.1.220") == ["2.1.220", "2.1.235"]


def test_the_menu_keeps_the_image_order() -> None:
    """First is the default — a meaning, not an accident, so it is not sorted."""
    assert _split_menu("2.1.220, 2.1.235 ,", "2.1.220") == ["2.1.220", "2.1.235"]


# ── What the sandbox actually executes ───────────────────────────────────


class _Backend:
    """Only what version selection reads off a sandbox backend."""

    def __init__(self, available: dict[str, list[str]] | None) -> None:
        self._available = available

    def version_stamp(self) -> dict[str, Any]:
        return {"runtime_versions_available": self._available}


def _job(version: str | None) -> ClaimedJob:
    return ClaimedJob(
        job_id="ajob_1",
        attempt_id=1,
        attempt_no=1,
        repo="owner/name",
        base_sha=None,
        task_prompt="hi",
        runtime="claude-code",
        model="m",
        worker_token="w",
        sandbox_token="s",
        runtime_version=version,
    )


def test_the_selected_version_goes_on_the_front_of_path() -> None:
    """Every runtime resolves its binary by name, so PATH is the whole switch."""
    env = {"PATH": SANDBOX_PATH}
    _select_runtime_version(env, job=_job("2.1.235"), backend=_Backend(_MENU))

    assert env["PATH"] == f"{runtime_bin_dir('claude-code', '2.1.235')}:{SANDBOX_PATH}"
    # The base directories survive: the agent still needs git, node and the
    # wrappers, which live where they always did.
    assert env["PATH"].endswith(SANDBOX_PATH)


def test_no_version_leaves_path_alone() -> None:
    """A control plane that predates selection means "the image default"."""
    env = {"PATH": SANDBOX_PATH}
    _select_runtime_version(env, job=_job(None), backend=_Backend(_MENU))

    assert env["PATH"] == SANDBOX_PATH


def test_a_version_this_image_lacks_refuses_instead_of_falling_back() -> None:
    """The failure this whole seam exists to prevent.

    Without the versioned directory the binary name still resolves — to the
    image's symlinked default — so the job would run one version while its
    attempt row records another. Nothing downstream could notice.
    """
    env = {"PATH": SANDBOX_PATH}
    with pytest.raises(SandboxError) as caught:
        _select_runtime_version(env, job=_job("9.9.9"), backend=_Backend(_MENU))

    assert "9.9.9" in str(caught.value)
    assert env["PATH"] == SANDBOX_PATH, "PATH must not be touched on the refusal path"


def test_the_reason_is_reportable_before_the_agent_starts() -> None:
    """``run_once`` fails the attempt with this rather than letting it time out."""
    assert runtime_version_unavailable(_job("9.9.9"), _Backend(_MENU)) is not None
    assert runtime_version_unavailable(_job("2.1.235"), _Backend(_MENU)) is None


def test_an_image_that_reports_no_menu_is_trusted() -> None:
    """A host too old to report a menu is not a host that can run nothing.

    It reports no menu and its claims carry no selection, so the only version
    that can reach it is one it itself reported as the default.
    """
    env = {"PATH": SANDBOX_PATH}
    _select_runtime_version(env, job=_job("2.1.220"), backend=_Backend(None))

    assert env["PATH"].startswith(runtime_bin_dir("claude-code", "2.1.220"))
