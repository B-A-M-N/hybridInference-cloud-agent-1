"""The readiness check tells the four failures apart.

Each of them currently arrives as a 404, and reading the status code alone is
how the routing gap was misdiagnosed the first time: `/internal/model-catalog`
answered 404 with the frontend's HTML, and that was written up as evidence
about the dispatch token. It was evidence the request never reached the
backend.

So the judgements are pure functions over `(status, body)` and tested here.
The network part is a thin wrapper with nothing to decide.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "deploy" / "preflight" / "check_gateway.py"
_spec = importlib.util.spec_from_file_location("check_gateway", _PATH)
assert _spec and _spec.loader
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

HTML_404 = "<!DOCTYPE html><html><head><title>404: This page could not be found</title>"


def _gateway_error(kind: str) -> str:
    return json.dumps({"error": {"type": kind, "message": "..."}})


# ---------------------------------------------------------------------------
# The distinction the first probe missed
# ---------------------------------------------------------------------------


def test_the_frontends_404_is_not_the_gateways() -> None:
    """Same status, opposite meanings, and only the body says which."""
    frontend = check.judge_routing(404, HTML_404)
    backend = check.judge_routing(404, _gateway_error("internal_api_not_configured"))
    assert frontend.state == check.FAIL
    assert "not forwarded" in frontend.remedy
    assert backend.state == check.OK


def test_an_unauthorized_json_refusal_proves_the_backend_answered() -> None:
    """A refusal is the *positive* result for the routing check.

    Only the backend produces one, so this is checked without a token on
    purpose — needing a credential to find out whether a path is routed would
    conflate two questions.
    """
    assert check.judge_routing(401, _gateway_error("unauthorized")).state == check.OK


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def test_an_unconfigured_key_is_named_as_such() -> None:
    verdict = check.judge_jwks(404, _gateway_error("identity_not_configured"))
    assert verdict.state == check.FAIL
    assert "IDENTITY_JWT_PRIVATE_KEY" in verdict.remedy


def test_a_broken_key_is_not_reported_as_a_missing_one() -> None:
    """The gateway answers 500 for a key it cannot use, deliberately.

    A deployment with a malformed PEM has a different problem from one with no
    PEM, and telling an operator to set a variable they already set is how an
    afternoon goes.
    """
    verdict = check.judge_jwks(500, "")
    assert verdict.state == check.FAIL
    assert "cannot be used" in verdict.detail


def test_a_published_key_passes_and_an_empty_set_does_not() -> None:
    assert check.judge_jwks(200, json.dumps({"keys": [{"kid": "a"}]})).state == check.OK
    assert check.judge_jwks(200, json.dumps({"keys": []})).state == check.FAIL


def test_html_from_the_jwks_path_is_not_read_as_a_key_problem() -> None:
    """Wrong origin entirely — say that rather than blame the key."""
    verdict = check.judge_jwks(404, HTML_404)
    assert "not serving the gateway's API" in verdict.remedy


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "body", "expected", "phrase"),
    [
        (200, "{}", check.OK, ""),
        (403, _gateway_error("subject_unavailable"), check.OK, ""),
        (401, _gateway_error("unauthorized"), check.FAIL, "does not match"),
        (
            404,
            _gateway_error("internal_api_not_configured"),
            check.FAIL,
            "no dispatch token set",
        ),
    ],
)
def test_authorization_outcomes(status: int, body: str, expected: str, phrase: str) -> None:
    """403 is a pass: the token was accepted and the probe user is not real."""
    verdict = check.judge_internal_api(status, body)
    assert verdict.state == expected
    assert phrase in (verdict.detail + verdict.remedy)


# ---------------------------------------------------------------------------
# Exit code
# ---------------------------------------------------------------------------


def test_a_failure_exits_non_zero(monkeypatch, capsys) -> None:
    """So it can gate a deploy step rather than only inform a human."""
    monkeypatch.setattr(
        check,
        "run",
        lambda origin, token: [("x", check.Verdict(check.FAIL, "d", "r"))],
    )
    assert check.main(["https://gw.test"]) == 1
    assert "not ready" in capsys.readouterr().out


def test_skips_alone_do_not_fail(monkeypatch, capsys) -> None:
    """An unset token here is a gap in the check, not a fault in the gateway."""
    monkeypatch.setattr(
        check,
        "run",
        lambda origin, token: [
            ("a", check.Verdict(check.OK, "d")),
            ("b", check.Verdict(check.SKIP, "d", "r")),
        ],
    )
    assert check.main(["https://gw.test"]) == 0
    assert "were skipped" in capsys.readouterr().out


def test_the_token_is_never_taken_from_argv(monkeypatch) -> None:
    """`argv` is readable by every process on the host."""
    seen: dict[str, str] = {}
    monkeypatch.setattr(
        check,
        "run",
        lambda origin, token: seen.update(origin=origin, token=token) or [],
    )
    monkeypatch.setenv(check.ENV_TOKEN, "from-the-environment")
    assert check.main(["https://gw.test"]) == 2  # no checks ran
    assert seen["token"] == "from-the-environment"
    # Two arguments is a usage error, not a second way to pass the token.
    assert check.main(["https://gw.test", "a-token"]) == 2
