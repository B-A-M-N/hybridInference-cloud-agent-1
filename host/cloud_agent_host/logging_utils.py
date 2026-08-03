"""Module loggers for the host package.

The gateway's ``serving.utils.logging`` couples ``get_logger`` to its JSON
formatter and per-request context machinery — none of which exists on a host.
The moved modules only ever call ``get_logger(__name__)``, so this shim keeps
exactly that surface: a stdlib logger, configured by whatever entrypoint runs
the host process. Copied-not-imported per the migration rule that the gateway
is reached over HTTP only.
"""

from __future__ import annotations

import logging


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a module logger; handler configuration belongs to the entrypoint."""
    return logging.getLogger(name or __name__)
