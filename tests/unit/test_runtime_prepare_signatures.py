"""Every registered runtime accepts the call the runner actually makes.

`KiloRuntime.prepare` did not take `provides_isolation`, which the runner
passes unconditionally. Every Kilo job died with a `TypeError` before the
sandbox started — and nothing caught it, because each runtime's tests call
`prepare` with the arguments that runtime happens to want. A signature can
only drift from its caller if something checks them against each other, and
nothing did.

So this calls `prepare` on every id in the registry with the exact keyword set
`_run_agent` uses. It is a compile-time check written as a test, which is what
Python leaves you when a base class declares an interface and a subclass
narrows it silently.

The keyword list is duplicated from the call site on purpose. Importing it, or
deriving it by introspection, would make this test agree with the runner by
construction — including when both are wrong together.
"""

from __future__ import annotations

import pytest

from cloud_agent_host.runtimes import (
    EMPTY_RUNTIME_MCP_CONFIG,
    get_runtime,
    registered_runtimes,
)

#: Exactly what `runner._run_agent` passes. Keep in step with that call by
#: hand — see the module docstring for why this is not imported.
_RUNNER_KWARGS = {
    "workdir": "/workspace",
    "task_prompt": "do the thing",
    "model": "glm-5.1",
    "gateway_base_url": "http://gateway:8080",
    "credential": "agr.fake.fake",
    "provides_isolation": False,
    "mcp_config": EMPTY_RUNTIME_MCP_CONFIG,
}


@pytest.mark.parametrize("name", registered_runtimes())
def test_prepare_accepts_the_runners_call(name: str) -> None:
    """A registered runtime cannot refuse the only call it will ever get."""
    argv, env = get_runtime(name).prepare(**_RUNNER_KWARGS)
    assert argv, f"{name} produced no command"
    assert isinstance(env, dict)


@pytest.mark.parametrize("name", registered_runtimes())
def test_prepare_honours_isolation(name: str) -> None:
    """Passing the flag changes nothing structural — it must not crash.

    The value's *meaning* is per-runtime (Codex drops to
    `danger-full-access` inside a container backend, others ignore it). What
    is uniform is that both values are callable, so a runtime cannot accept
    the keyword and then reject one of its two possible values.
    """
    for isolation in (True, False):
        argv, _ = get_runtime(name).prepare(**{**_RUNNER_KWARGS, "provides_isolation": isolation})
        assert argv
