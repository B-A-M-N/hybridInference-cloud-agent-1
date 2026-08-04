#!/usr/bin/env python3
"""Ask a gateway whether it is ready to be cut over to.

Four things have to be true before section 3 of `docs/smoke.md` can start, and
each of them currently fails in a way that reads as something else:

* **No identity key.** `/v1/identity/jwks` answers 404. A 404 from a route that
  exists reads as a route that does not, so this reports the difference.
* **`/internal/*` not forwarded.** Nginx sends unmatched paths to the frontend,
  whose 404 is an HTML page. A control plane reading that concludes the gateway
  is down. The rewrites landed in the gateway's #1202; this is how you tell
  whether the deployment carrying them is live yet.
* **No dispatch token.** Every internal path answers 404
  `internal_api_not_configured` — correct, and indistinguishable from a build
  with no internal API.
* **A dispatch token that does not match.** 401, which at least says so.

Run it after setting the variables and before scheduling anything:

    python deploy/preflight/check_gateway.py https://staging.freeinference.org

With a token, so the authorized paths are exercised too:

    GATEWAY_GRANT_DISPATCH_TOKEN=... python deploy/preflight/check_gateway.py <url>

**The token is read from the environment, never from a flag.** `argv` is
readable by every process on the host.

Read-only: it fetches JWKS and reads a catalog. It mints nothing and changes
nothing, so it is safe against production.

Exit code is 0 only when every check passes, so it can gate a deploy step.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, NamedTuple

import httpx

ENV_TOKEN = "GATEWAY_GRANT_DISPATCH_TOKEN"

OK = "pass"
FAIL = "fail"
SKIP = "skip"


class Verdict(NamedTuple):
    """One check's outcome and what to do about it."""

    state: str
    detail: str
    remedy: str = ""


def _looks_like_html(body: str) -> bool:
    """Whether a response body is a web page rather than an API answer.

    The distinction the original probe missed. A gateway that refuses says so
    in JSON; a frontend that never forwarded the request answers in HTML, and
    both arrive as 404.
    """
    head = body.lstrip()[:200].lower()
    return head.startswith("<!doctype") or head.startswith("<html") or "<title>" in head


def _error_type(body: str) -> str:
    """The gateway's error type, or an empty string if this is not one."""
    try:
        payload = json.loads(body)
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if not isinstance(error, dict):
        return ""
    return str(error.get("type") or "")


def judge_jwks(status: int, body: str) -> Verdict:
    """Whether this gateway can sign identity tokens."""
    if status == 200:
        try:
            keys = json.loads(body).get("keys")
        except ValueError:
            return Verdict(FAIL, "JWKS is not JSON", "check what is fronting this origin")
        if not isinstance(keys, list) or not keys:
            return Verdict(FAIL, "JWKS is empty", "IDENTITY_JWT_PRIVATE_KEY is set but has no key")
        return Verdict(OK, f"{len(keys)} signing key(s) published")
    if _error_type(body) == "identity_not_configured":
        return Verdict(
            FAIL,
            "identity is not configured",
            "set IDENTITY_JWT_PRIVATE_KEY, IDENTITY_ISSUER and "
            "IDENTITY_ALLOWED_REDIRECTS on the gateway",
        )
    if status == 500:
        return Verdict(
            FAIL,
            "the configured key cannot be used",
            "a malformed, encrypted, non-RSA or undersized PEM — the gateway "
            "reports this as a server error on purpose, so it is not mistaken "
            "for 'not configured'",
        )
    if _looks_like_html(body):
        return Verdict(FAIL, f"HTML at {status}", "this origin is not serving the gateway's API")
    return Verdict(FAIL, f"unexpected {status}", body[:120])


def judge_routing(status: int, body: str) -> Verdict:
    """Whether `/internal/*` reaches the backend at all.

    Answered without a token on purpose: an unauthorized *JSON* refusal is the
    proof, because only the backend produces one.
    """
    if _looks_like_html(body):
        return Verdict(
            FAIL,
            f"the frontend answered ({status}, HTML)",
            "/internal/model-catalog is not forwarded to the backend — the "
            "rewrites in the gateway's #1202 are not on this deployment yet",
        )
    kind = _error_type(body)
    if kind in ("unauthorized", "internal_api_not_configured") or status in (200, 401, 403, 404):
        return Verdict(OK, f"the backend answered ({status}, JSON)")
    return Verdict(FAIL, f"unexpected {status}", body[:120])


def judge_internal_api(status: int, body: str) -> Verdict:
    """Whether the dispatch token this environment holds is the gateway's."""
    kind = _error_type(body)
    if status == 200:
        return Verdict(OK, "the catalog answered")
    if kind == "internal_api_not_configured":
        return Verdict(
            FAIL,
            "the gateway has no dispatch token set",
            f"set {ENV_TOKEN} on the gateway to the same value used here, and "
            "to a different value in every environment",
        )
    if status == 401:
        return Verdict(
            FAIL,
            "the dispatch token does not match",
            "the gateway has one set; it is not this one",
        )
    if status == 403:
        # The token was accepted. The user id is a probe value.
        return Verdict(OK, "authorized (the probe user is unknown, which is expected)")
    if _looks_like_html(body):
        return Verdict(FAIL, f"the frontend answered ({status})", "see the routing check above")
    return Verdict(FAIL, f"unexpected {status}", body[:120])


def _get(client: httpx.Client, url: str, **kwargs: Any) -> tuple[int, str]:
    try:
        response = client.get(url, **kwargs)
    except httpx.HTTPError as exc:
        return 0, f"unreachable: {exc}"
    return response.status_code, response.text


def run(origin: str, token: str) -> list[tuple[str, Verdict]]:
    """Run every check against one gateway origin."""
    origin = origin.rstrip("/")
    results: list[tuple[str, Verdict]] = []
    with httpx.Client(timeout=15.0, follow_redirects=True) as client:
        status, body = _get(client, f"{origin}/v1/identity/jwks")
        results.append(("identity signing key", judge_jwks(status, body)))

        status, body = _get(client, f"{origin}/internal/model-catalog")
        results.append(("/internal/* reaches the backend", judge_routing(status, body)))

        if not token:
            results.append(
                (
                    "internal API authorization",
                    Verdict(SKIP, f"{ENV_TOKEN} not set here", f"re-run with {ENV_TOKEN} exported"),
                )
            )
        else:
            status, body = _get(
                client,
                f"{origin}/internal/model-catalog",
                params={"user_id": "preflight-probe"},
                headers={"Authorization": f"Bearer {token}"},
            )
            results.append(("internal API authorization", judge_internal_api(status, body)))
    return results


def main(argv: list[str] | None = None) -> int:
    """Check one origin and report. Returns 0 only if nothing failed."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print(__doc__.strip().split("\n\n")[0], file=sys.stderr)
        print(f"\nusage: {sys.argv[0]} <gateway-origin>", file=sys.stderr)
        return 2

    origin = args[0]
    results = run(origin, os.environ.get(ENV_TOKEN, "").strip())

    print(f"\n{origin}\n")
    if not results:
        print("no checks ran", file=sys.stderr)
        return 2
    width = max(len(name) for name, _ in results)
    for name, verdict in results:
        mark = {OK: "  ok  ", FAIL: " FAIL ", SKIP: " skip "}[verdict.state]
        print(f"[{mark}] {name:<{width}}  {verdict.detail}")
        if verdict.remedy and verdict.state != OK:
            print(f"{'':>{width + 11}}{verdict.remedy}")

    failed = [name for name, verdict in results if verdict.state == FAIL]
    skipped = [name for name, verdict in results if verdict.state == SKIP]
    print()
    if failed:
        print(f"not ready: {len(failed)} check(s) failed")
    elif skipped:
        print("everything checked passed; some checks were skipped")
    else:
        print("ready for section 3 of docs/smoke.md")
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
