"""Module loggers for the host package.

The gateway's ``serving.utils.logging`` couples ``get_logger`` to its JSON
formatter and per-request context machinery — none of which exists on a host.
The moved modules only ever call ``get_logger(__name__)``, so this shim keeps
exactly that surface. Copied-not-imported per the migration rule that the
gateway is reached over HTTP only.

**Known behavioural difference, deliberately deferred.** The source's
``get_logger`` also *initialised* logging as a side effect (root handler,
level from env). This shim does not, so with no entrypoint configuration the
root logger sits at WARNING with zero handlers and INFO events such as
``agent_sandbox_spawn`` go nowhere. That is acceptable exactly as long as this
package has no runnable entrypoint. **D6 (the runner move) owns closing it:**
its acceptance includes configuring logging at the entrypoint and a test that
a host module's INFO event actually reaches a handler. Tracked in
MIGRATION.md's deferred-behaviour register; ``tests/unit/test_logging_utils.py``
pins today's no-side-effects contract so the gap cannot close by accident and
go unnoticed.
"""

from __future__ import annotations

import logging


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a module logger; handler configuration belongs to the entrypoint."""
    return logging.getLogger(name or __name__)
