"""Server-owned execution profiles for sandbox scheduling.

An execution profile is a platform policy, not job metadata.  Callers choose
what work to run; the deployment chooses the minimum boundary that work must
run behind.  Keeping the value in its own column makes that distinction
enforceable at claim time and inheritable across a thread.
"""

from __future__ import annotations

import os
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


class ExecutionProfile(StrEnum):
    """The execution boundaries the scheduler currently understands."""

    TRUSTED_V1 = "trusted-v1"
    UNTRUSTED_KATA_V1 = "untrusted-kata-v1"


ENV_DEFAULT_EXECUTION_PROFILE = "AGENT_DEFAULT_EXECUTION_PROFILE"
KATA_RUNTIME = "io.containerd.kata.v2"
RESERVED_EXECUTION_METADATA_KEYS = frozenset(
    {
        "execution_profile",
        "_agent_execution_profile",
        "sandbox_runtime",
        "sandbox_image_digest",
        "runtime_versions",
        "host_kernel",
        "guest_kernel",
    }
)


def default_execution_profile(env: Mapping[str, str] | None = None) -> ExecutionProfile:
    """Return the deployment policy for newly created root threads.

    Existing deployments default to ``trusted-v1`` so upgrading the control
    plane cannot strand their queue.  Opting into public/untrusted work is an
    explicit deployment change, and an invalid value refuses job creation
    instead of silently weakening to the trusted profile.
    """
    source = env if env is not None else os.environ
    raw = (source.get(ENV_DEFAULT_EXECUTION_PROFILE) or ExecutionProfile.TRUSTED_V1).strip()
    try:
        return ExecutionProfile(raw)
    except ValueError as exc:
        allowed = ", ".join(profile.value for profile in ExecutionProfile)
        raise ValueError(
            f"{ENV_DEFAULT_EXECUTION_PROFILE} must be one of: {allowed}; got {raw!r}"
        ) from exc
