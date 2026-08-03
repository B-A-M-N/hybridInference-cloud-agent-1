"""Pins the logging shim's contract — including the gap D6 must close.

The source repo's ``get_logger`` initialised root logging as a side effect;
the host shim deliberately does not. These tests make that difference a stated
contract instead of an accident: if someone adds side-effect configuration to
the shim, or D6 forgets to configure logging at the runner entrypoint, one of
these assertions (or D6's own required test, see the module docstring) is the
tripwire.
"""

from __future__ import annotations

import logging

from cloud_agent_host.logging_utils import get_logger


def test_returns_the_named_stdlib_logger() -> None:
    logger = get_logger("cloud_agent_host.sandbox")
    assert isinstance(logger, logging.Logger)
    assert logger.name == "cloud_agent_host.sandbox"
    assert logger is logging.getLogger("cloud_agent_host.sandbox")


def test_configures_nothing_as_a_side_effect() -> None:
    """The shim must not touch global logging state.

    Handler/level configuration belongs to the process entrypoint (task D6 for
    the runner). If this test starts failing because the shim grew a root
    handler, that configuration moved to import time — which breaks any
    embedding process that configures logging itself.
    """
    root_handlers_before = list(logging.getLogger().handlers)
    root_level_before = logging.getLogger().level

    get_logger("cloud_agent_host.probe")

    assert list(logging.getLogger().handlers) == root_handlers_before
    assert logging.getLogger().level == root_level_before


def test_unconfigured_info_events_are_silent_which_is_the_d6_gap() -> None:
    """Documents the deferred behaviour, so it is visible, not latent.

    With no entrypoint configuration, INFO from host modules is not handled
    (WARNING-level root, no handlers). D6's runner entrypoint must configure
    logging and add the positive test proving INFO events emit; when it does,
    this test still holds — it asserts the *shim* adds nothing, not that the
    process never can.
    """
    logger = get_logger("cloud_agent_host.sandbox")
    # No handlers of its own, and root's default threshold hides INFO.
    assert logger.handlers == []
    assert not logger.isEnabledFor(logging.INFO) or logging.getLogger().handlers
