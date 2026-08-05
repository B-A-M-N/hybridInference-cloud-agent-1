"""The reverse-proxy snippet keeps the properties that are easy to lose.

`deploy/reverse-proxy/agents-own-origin.conf` is applied by hand — the proxy in
front of these hosts is not version-controlled anywhere — so this file is the
reviewed text an operator pastes. Several details in it fail quietly:

**The port is the security boundary.** The gateway keeps its access token in
`sessionStorage`, which is scoped to an origin and nothing finer, and this app
renders model output and terminal contents. Serving them on one origin lets
untrusted bytes reach that token. A listener that drifts back onto the
gateway's port undoes it silently — nothing breaks, everything keeps working.

**Prefix stripping is a trailing slash.** `proxy_pass http://host:8000/`
replaces the matched location with `/`; `proxy_pass http://host:8000` forwards
it intact. Drop the slash and every control-plane route 404s, which reads as
the service being down rather than as a proxy fault.

**SSE dies with buffering on.** nginx holds the response until its buffer
fills or the request ends, so job output arrives in one lump at the end, or
never. A run looks hung while it is working, which is indistinguishable from
a stuck sandbox.

Textual, and deliberately so: this is a config file no test can execute, and
the alternative to checking the text is checking nothing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CONF = Path(__file__).resolve().parents[2] / "deploy" / "reverse-proxy" / "agents-own-origin.conf"

#: The gateway's own listeners. This stack must not share one.
GATEWAY_PORTS = {"80", "443"}


def _block(header: str) -> str:
    """Return the body of `<header> { ... }`, braces balanced."""
    text = CONF.read_text()
    start = text.index(f"{header} {{")
    depth, i = 0, start
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
        i += 1
    raise AssertionError(f"unterminated {header}")


def _listen_ports() -> set[str]:
    return set(re.findall(r"^\s*listen\s+(?:\[::\]:)?(\d+)", CONF.read_text(), re.MULTILINE))


# ---------------------------------------------------------------------------
# The origin boundary
# ---------------------------------------------------------------------------


def test_the_agent_stack_does_not_share_the_gateway_s_port() -> None:
    """The whole reason this is a server block and not an include.

    An origin is scheme + host + port. Sharing the gateway's port makes this
    app same-origin with it, and same-origin means its scripts can read the
    gateway's `sessionStorage` — where the gateway's access token lives. This
    app's normal input is model output and terminal contents, so that is not a
    theoretical reach.

    The earlier shape mounted it at `/agents` on the gateway's own port. A path
    prefix is not a boundary, and nothing about the deployment would have
    looked wrong.
    """
    ports = _listen_ports()
    assert ports, "no listen directive: this must be its own server block"
    assert not (ports & GATEWAY_PORTS), (
        f"listening on {sorted(ports & GATEWAY_PORTS)} puts this app on the "
        "gateway's origin, where it can read the gateway's sessionStorage"
    )


def test_the_file_does_not_claim_the_cookie_is_contained() -> None:
    """A port separates storage and the DOM. It does not separate cookies.

    RFC 6265: cookies are not isolated by port, so the gateway's `Path=/`
    refresh cookie is still attached to requests to this origin. It is
    `HttpOnly` and therefore unreadable here, but the control plane receives
    it, and closing that half is a gateway-side change to the cookie's `Path`.

    Pinned as text because the risk is a later edit that tidies the caveat away
    and leaves a reader believing the boundary is complete.
    """
    text = CONF.read_text().lower()
    assert "cookies ignore the port" in text
    assert "rfc 6265" in text


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def test_the_api_location_strips_its_prefix() -> None:
    """Without the trailing slash the control plane sees `/api/...`."""
    upstream = re.search(r"proxy_pass\s+(\S+);", _block("location /api/"))
    assert upstream, "no proxy_pass in the api location"
    assert upstream.group(1).endswith("/"), (
        "proxy_pass must end in `/` so nginx replaces the matched prefix; "
        f"got {upstream.group(1)!r}, which forwards /api/ to the control "
        "plane and 404s every route"
    )


def test_the_web_app_is_served_from_the_root_of_this_origin() -> None:
    """`NEXT_PUBLIC_BASE_PATH` is empty for this shape, so nothing is stripped.

    Next generates unprefixed links and asset URLs when the base path is
    empty. A `proxy_pass` with a trailing slash here would strip a prefix that
    was never added, which breaks every asset while the HTML still loads.
    """
    upstream = re.search(r"proxy_pass\s+(\S+);", _block("location /"))
    assert upstream, "no proxy_pass in the web location"
    assert not upstream.group(1).endswith("/"), (
        f"proxy_pass must NOT end in `/` here; got {upstream.group(1)!r}"
    )


@pytest.mark.parametrize(
    "directive",
    ["proxy_buffering off;", "proxy_cache off;", "X-Accel-Buffering no"],
)
def test_the_api_location_does_not_buffer(directive: str) -> None:
    """Buffered SSE is a job that looks hung until it finishes."""
    assert directive in _block("location /api/"), (
        f"{directive!r} is missing; job output would arrive in one lump at "
        "the end of the run, or not at all"
    )


def test_a_stream_is_not_closed_for_being_idle() -> None:
    """The default 60s read timeout cuts a healthy stream mid-tool-call."""
    timeout = re.search(r"proxy_read_timeout\s+(\d+)s;", _block("location /api/"))
    assert timeout and int(timeout.group(1)) >= 600, (
        "proxy_read_timeout must outlast a quiet stretch in a stream; the "
        "default closes healthy connections during a long tool call"
    )
