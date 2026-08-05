"""Joining the pool, and staying in it.

The control plane has had `/v1/agent/host/register` and
`/v1/agent/host/heartbeat` since phase G. Nothing on this side called either,
so every consequence that hangs off a host row was inert:

* **capacity** — `slots_total` was never reported, so the scheduler had no
  number to respect and a host took work without limit;
* **offline** — the reaper moves hosts that stop heartbeating to `offline`,
  which with no heartbeats meant either every host or none, depending on
  whether a row existed at all;
* **drain and remove** — an operator's decision the runner never read;
* **broker address** — the row carries the `broker_url` from which file,
  terminal and git requests are served, and a row without one answers 409 for
  every one of them.

**The credential is per host, and it is not the dispatcher token.** That token
opens the claim door for every machine in the pool; this one opens it for
exactly one, and removing the host revokes it. A pool sharing one secret has
no way to retire a single compromised machine.

Enrollment is a one-time token spent for a lasting credential. A machine being
set up has none yet, which is the whole reason the exchange exists; the token
names the host it may register as, so a typo cannot revoke a live machine's
credential.

**The credential is written to a file, never logged and never passed in
`argv`.** `/proc/<pid>/cmdline` is world-readable, and this host runs untrusted
repository code next door.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cloud_agent_host.logging_utils import get_logger

logger = get_logger(__name__)

ENV_ENROLL_TOKEN = "AGENT_ENROLL_TOKEN"
ENV_HOST_CREDENTIAL = "AGENT_HOST_CREDENTIAL"
ENV_CREDENTIAL_FILE = "AGENT_HOST_CREDENTIAL_FILE"

#: Where a credential is kept when the deployment names no path. Under the
#: workdir root so it shares that volume's lifetime: a credential that outlives
#: the machine it identifies is a credential nobody will think to revoke.
DEFAULT_CREDENTIAL_FILE = "/var/lib/hybridinference/agent-host-credential"

#: Fallback cadence, used only until a heartbeat answers. The control plane
#: tells us its own interval, and following that rather than a local constant
#: is what keeps "missed heartbeats" meaning the same thing on both sides.
DEFAULT_HEARTBEAT_INTERVAL_S = 30.0

#: A heartbeat is a small write; something is wrong long before this expires.
_TIMEOUT_S = 15.0


class EnrollmentError(RuntimeError):
    """This machine cannot join, or cannot stay in, the pool."""


class HostRemoved(EnrollmentError):
    """The control plane no longer recognises this host.

    Distinct from a network failure on purpose: a removed host must stop
    claiming, while an unreachable control plane means keep the work already
    in flight and try again.
    """


@dataclass
class HostIdentity:
    """What this machine is in the pool, and what it authenticates with."""

    host: str
    credential: str
    status: str = "active"
    #: Seconds between heartbeats, as the control plane last reported them.
    interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S
    slots_total: int | None = None
    capabilities: list[str] = field(default_factory=list)


def credential_path() -> Path:
    """Where this deployment keeps its host credential."""
    return Path(os.environ.get(ENV_CREDENTIAL_FILE) or DEFAULT_CREDENTIAL_FILE)


def load_credential() -> str:
    """Return the stored credential, or an empty string.

    The environment wins over the file, so a deployment that injects the
    credential by another mechanism needs no file at all.
    """
    from_env = (os.environ.get(ENV_HOST_CREDENTIAL) or "").strip()
    if from_env:
        return from_env
    path = credential_path()
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def store_credential(credential: str) -> None:
    """Persist a credential for the next start.

    Mode 0600 before anything is written, not after: a credential that exists
    world-readable for even an instant on a machine running untrusted code has
    been readable.
    """
    path = credential_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive create then replace, so a concurrent reader never sees a
    # half-written file and no umask can widen the mode.
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, credential.encode())
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _post(url: str, body: dict[str, Any], *, bearer: str = "") -> dict[str, Any]:
    """POST JSON and return the parsed answer.

    Raises:
        HostRemoved: On 401/403 — the credential is not, or is no longer, one
            this control plane accepts.
        EnrollmentError: On anything else.
    """
    payload = json.dumps(body).encode()
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if bearer:
        request.add_header("Authorization", f"Bearer {bearer}")
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300].decode(errors="replace")
        if exc.code in (401, 403):
            raise HostRemoved(f"{exc.code} from {url}: {detail}") from exc
        raise EnrollmentError(f"{exc.code} from {url}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise EnrollmentError(f"{url} unreachable: {exc}") from exc


def register(
    *,
    base_url: str,
    host: str,
    enroll_token: str,
    broker_url: str | None = None,
    slots_total: int | None = None,
    capabilities: list[str] | None = None,
    runner_version: str | None = None,
) -> HostIdentity:
    """Spend an enrollment token for this machine's lasting credential."""
    answer = _post(
        f"{base_url.rstrip('/')}/v1/agent/host/register",
        {
            "enroll_token": enroll_token,
            "name": host,
            "broker_url": broker_url,
            "slots_total": slots_total,
            "capabilities": capabilities or [],
            "runner_version": runner_version,
        },
    )
    credential = str(answer.get("credential") or "")
    if not credential:
        raise EnrollmentError("registration returned no credential")
    identity = HostIdentity(
        host=str(answer.get("host") or host),
        credential=credential,
        status=str(answer.get("status") or "active"),
    )
    store_credential(credential)
    logger.info(
        "agent_host_registered",
        extra={"event": "agent_host_registered", "host": identity.host, "status": identity.status},
    )
    return identity


def heartbeat(
    *,
    base_url: str,
    identity: HostIdentity,
    slots_total: int | None = None,
    broker_url: str | None = None,
    capabilities: list[str] | None = None,
    runner_version: str | None = None,
) -> HostIdentity:
    """Report liveness and capacity, and read back what the pool decided.

    The answer carries the operator's side of the conversation — ``draining``,
    ``offline``, a changed interval — which is the only way a runner learns
    that a human asked it to stop taking work.
    """
    answer = _post(
        f"{base_url.rstrip('/')}/v1/agent/host/heartbeat",
        {
            "slots_total": slots_total,
            "broker_url": broker_url,
            "capabilities": capabilities,
            "runner_version": runner_version,
        },
        bearer=identity.credential,
    )
    identity.status = str(answer.get("status") or identity.status)
    interval = answer.get("heartbeat_interval_seconds")
    if isinstance(interval, (int, float)) and interval > 0:
        identity.interval_s = float(interval)
    reported = answer.get("slots_total")
    identity.slots_total = int(reported) if isinstance(reported, int) else identity.slots_total
    return identity


def ensure_enrolled(
    *,
    base_url: str,
    host: str,
    broker_url: str | None = None,
    slots_total: int | None = None,
    capabilities: list[str] | None = None,
    runner_version: str | None = None,
) -> HostIdentity | None:
    """Return this machine's identity, enrolling if it has a token and no credential.

    ``None`` means this deployment does not use the pool — neither a stored
    credential nor an enrollment token — which is the single-host shape that
    worked before phase G and must keep working. Enrolment is opt-in; a runner
    that has never heard of it is not broken.
    """
    credential = load_credential()
    if credential:
        return HostIdentity(host=host, credential=credential)

    token = (os.environ.get(ENV_ENROLL_TOKEN) or "").strip()
    if not token:
        return None

    return register(
        base_url=base_url,
        host=host,
        enroll_token=token,
        broker_url=broker_url,
        slots_total=slots_total,
        capabilities=capabilities,
        runner_version=runner_version,
    )


class HostHeartbeat:
    """Heartbeats on a daemon thread for as long as the runner is up.

    Modelled on the per-job lease heartbeat that already exists, and separate
    from it because they answer different questions: that one says "this
    attempt is still mine", this one says "this machine is still here". A host
    with no jobs must still report, or the reaper marks it offline and stops
    sending it work — which is a pool that empties whenever it is idle.
    """

    def __init__(
        self,
        *,
        base_url: str,
        identity: HostIdentity,
        slots_total: int | None = None,
        broker_url: str | None = None,
        capabilities: list[str] | None = None,
        runner_version: str | None = None,
    ) -> None:
        self._base_url = base_url
        self._identity = identity
        self._slots_total = slots_total
        self._broker_url = broker_url
        self._capabilities = capabilities
        self._runner_version = runner_version
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: Set when the control plane says this host is gone. The runner reads
        #: it to stop claiming; it does not abandon work already in flight,
        #: because a job in progress has its own lease and its own fence.
        self.removed = threading.Event()

    @property
    def status(self) -> str:
        """What the pool last said about this host."""
        return self._identity.status

    @property
    def claimable(self) -> bool:
        """Whether this runner should ask for more work.

        `draining` is an operator saying "finish what you have and stop", so
        it is not an error and nothing is interrupted — but a runner that kept
        claiming through it would make draining mean nothing.
        """
        return not self.removed.is_set() and self._identity.status == "active"

    def start(self) -> None:
        """Begin heartbeating."""
        self._thread = threading.Thread(target=self._run, daemon=True, name="agent-host-heartbeat")
        self._thread.start()

    def stop(self) -> None:
        """Stop heartbeating and join."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                heartbeat(
                    base_url=self._base_url,
                    identity=self._identity,
                    slots_total=self._slots_total,
                    broker_url=self._broker_url,
                    capabilities=self._capabilities,
                    runner_version=self._runner_version,
                )
            except HostRemoved:
                # Definitive: this credential is not one the control plane
                # accepts. Stop claiming and stop asking — retrying a refusal
                # that names us cannot succeed, and the log line is what an
                # operator will look for after removing a host.
                self.removed.set()
                logger.warning(
                    "agent_host_credential_rejected",
                    extra={
                        "event": "agent_host_credential_rejected",
                        "host": self._identity.host,
                    },
                )
                return
            except EnrollmentError:
                # A blip, an outage, a restart. Keep the host and keep trying:
                # the control plane's own reaper decides when silence has gone
                # on long enough, and that decision is not ours to duplicate.
                logger.warning(
                    "agent_host_heartbeat_failed",
                    exc_info=True,
                    extra={"event": "agent_host_heartbeat_failed", "host": self._identity.host},
                )
            self._stop.wait(self._identity.interval_s)


def sleep_until(deadline: float) -> None:  # pragma: no cover - trivial
    """Sleep to an absolute time, ignoring how long the caller took."""
    remaining = deadline - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)
