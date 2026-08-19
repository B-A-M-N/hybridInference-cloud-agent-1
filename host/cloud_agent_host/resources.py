"""What a turn actually consumed, measured from outside the sandbox.

The platform runs every turn on a machine its user cannot see, so "why was
that slow" and "what did that cost in CPU" have had no answer but ssh. This
module is the measurement half of the observability design's third signal
class: a periodic sampler that reads one sandbox's kernel counters from the
host and rolls a closed window up into one set of numbers.

**Why from the host, and never from inside.** Every counter here comes from
the runner's own reads of ``cgroupfs`` and ``/proc``. Nothing is asked of the
sandbox, nothing runs inside it, and no agent-visible process participates —
so a turn cannot see that it is measured, cannot refuse, and cannot report
numbers of its own choosing.

**Why deltas, never absolutes.** A cgroup's counters are cumulative from its
creation, and a resident thread's sandbox can outlive the turn being measured.
Every value reported is the difference between the window's last and first
sample, so a second turn in the same sandbox is never charged for the first.
Peak memory is the maximum of *our own* samples for the same reason: the
kernel's ``memory.peak`` is a high-water mark since cgroup creation, which
would attribute an earlier turn's spike to this one.

**Why a missing number is not a zero.** A host whose backend cannot be read
(no cgroup v2, a container the runner cannot resolve, a Docker daemon inside
a VM as on macOS) yields counters of ``None`` and ``coverage`` of 0. A turn
too short to sample twice yields the same. Reporting zeros there would be a
lie a dashboard cannot detect, so the honest shape is carried all the way to
the row: `coverage` says how much of the window was actually observed.

Nothing here may fail a turn. Every read is defensive and every failure
degrades to a missing counter, because a measurement problem must never
become the user's problem.
"""

from __future__ import annotations

import contextlib
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from cloud_agent_host.logging_utils import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)

#: Bumped when the meaning of a reported number changes, so a stored rollup
#: can always be read back with the semantics it was written under.
SAMPLER_VERSION = "1"

#: One sample per second. The reads are a handful of small files — microseconds
#: of work — and a turn is measured in tens of seconds, so this is fine-grained
#: enough for per-tool windows (design §6) without being a load of its own.
SAMPLE_INTERVAL_S = 1.0

#: How long to keep asking the daemon where a container is before giving up.
#: `docker run` returns its client process well before the container exists,
#: and on a host that must first pull an image that gap is seconds, not
#: milliseconds — asking once at spawn time is asking too early, every time.
BIND_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class Sample:
    """One instant's counters. ``None`` means that backend was unreadable."""

    mono: float
    cpu_usec: int | None = None
    mem_bytes: int | None = None
    io_read_bytes: int | None = None
    io_write_bytes: int | None = None
    net_rx_bytes: int | None = None
    net_tx_bytes: int | None = None


def _read_text(path: Path) -> str | None:
    """Read one counter file, or None if it cannot be read.

    Absence is routine rather than exceptional: cgroup files appear and vanish
    with the container, and a sampler that raised on a exiting container would
    turn an ordinary race into a failed turn.
    """
    try:
        return path.read_text()
    except (OSError, ValueError):
        return None


def _parse_keyed(text: str, key: str) -> int | None:
    """Pull one ``key value`` line out of a cgroup stat file."""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == key:
            try:
                return int(parts[1])
            except ValueError:
                return None
    return None


def _parse_io_stat(text: str) -> tuple[int | None, int | None]:
    """Total read and written bytes across every device in ``io.stat``.

    Per-device rows are summed because the sandbox's io is one workload; which
    block device served it is a host fact, not something a turn's owner asked.
    """
    read_total: int | None = None
    write_total: int | None = None
    for line in text.splitlines():
        for field in line.split()[1:]:
            key, _, raw = field.partition("=")
            if key not in ("rbytes", "wbytes"):
                continue
            try:
                value = int(raw)
            except ValueError:
                continue
            if key == "rbytes":
                read_total = (read_total or 0) + value
            else:
                write_total = (write_total or 0) + value
    return read_total, write_total


def _parse_net_dev(text: str) -> tuple[int | None, int | None]:
    """Received and transmitted bytes for a namespace's real interfaces.

    ``lo`` is excluded: loopback is the sandbox talking to itself, which is
    not network the platform carries and would inflate a user-facing number
    with traffic that never left the machine.
    """
    rx_total: int | None = None
    tx_total: int | None = None
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        name = name.strip()
        if not rest or name in ("", "lo") or "|" in name:
            continue
        fields = rest.split()
        if len(fields) < 9:
            continue
        try:
            rx, tx = int(fields[0]), int(fields[8])
        except ValueError:
            continue
        rx_total = (rx_total or 0) + rx
        tx_total = (tx_total or 0) + tx
    return rx_total, tx_total


class ContainerCounters:
    """Reads one container's counters, given the pid the host sees for it.

    The cgroup path is resolved once from ``/proc/<pid>/cgroup``: it does not
    change for the life of the container, and re-deriving it every second
    would be a syscall spent to learn the same answer.
    """

    def __init__(
        self,
        pid: int,
        *,
        proc_root: Path | str = "/proc",
        cgroup_root: Path | str = "/sys/fs/cgroup",
    ) -> None:
        self._proc = Path(proc_root) / str(pid)
        self._cgroup_root = Path(cgroup_root)
        self._cgroup_line = _read_text(self._proc / "cgroup")
        self._cgroup_dir = self._resolve_cgroup()

    def _resolve_cgroup(self) -> Path | None:
        """Find this pid's cgroup v2 directory, or None where there is none.

        The v2 line is the one with an empty controller field (``0::<path>``).
        A v1-only host has no such line, and rather than half-measure across
        the two hierarchies the sampler reports nothing and says so through
        coverage.
        """
        text = _read_text(self._proc / "cgroup")
        if text is None:
            return None
        for line in text.splitlines():
            parts = line.split(":", 2)
            if len(parts) == 3 and parts[0] == "0" and parts[1] == "":
                candidate = self._cgroup_root / parts[2].lstrip("/")
                return candidate if candidate.is_dir() else None
        return None

    @property
    def readable(self) -> bool:
        """Whether anything at all can be measured for this container."""
        return self._cgroup_dir is not None or (self._proc / "net" / "dev").exists()

    def alive(self) -> bool:
        """Whether the pid still belongs to the container we resolved.

        A container that exits releases its pid, and the kernel hands that
        number to the next process the host starts — at which point
        ``/proc/<pid>/net/dev`` describes the *host's* interfaces. Reading it
        would report the machine's traffic since boot as this turn's network,
        a wrong number large enough to look deliberate. The cgroup directory
        disappears with the container, so its presence is the check; where
        there is no cgroup to watch, the pid's own cgroup line must still
        name the one we started from.
        """
        if self._cgroup_dir is not None:
            return self._cgroup_dir.is_dir()
        text = _read_text(self._proc / "cgroup")
        return text is not None and text == self._cgroup_line

    def read(self) -> Sample | None:
        """Take one sample, or None once the container is gone.

        None is a terminal answer, not a failed read: it means the thing
        being measured no longer exists, and every later number would belong
        to something else.
        """
        if not self.alive():
            return None
        cpu = mem = io_read = io_write = None
        if self._cgroup_dir is not None:
            cpu_text = _read_text(self._cgroup_dir / "cpu.stat")
            if cpu_text is not None:
                cpu = _parse_keyed(cpu_text, "usage_usec")
            mem_text = _read_text(self._cgroup_dir / "memory.current")
            if mem_text is not None:
                try:
                    mem = int(mem_text.strip())
                except ValueError:
                    mem = None
            io_text = _read_text(self._cgroup_dir / "io.stat")
            if io_text is not None:
                io_read, io_write = _parse_io_stat(io_text)
        rx = tx = None
        net_text = _read_text(self._proc / "net" / "dev")
        if net_text is not None:
            rx, tx = _parse_net_dev(net_text)
        return Sample(
            mono=time.monotonic(),
            cpu_usec=cpu,
            mem_bytes=mem,
            io_read_bytes=io_read,
            io_write_bytes=io_write,
            net_rx_bytes=rx,
            net_tx_bytes=tx,
        )


def _delta(samples: list[Sample], field: str) -> int | None:
    """Last minus first for one cumulative counter.

    ``None`` when fewer than two samples carried it: one reading of a
    cumulative counter says how much the cgroup has ever used, which is not
    what this window consumed. A counter that went backwards (a cgroup
    replaced under a resident sandbox) reports ``None`` rather than a
    negative or a wildly large number.
    """
    values = [getattr(s, field) for s in samples if getattr(s, field) is not None]
    if len(values) < 2:
        return None
    delta = values[-1] - values[0]
    return delta if delta >= 0 else None


def _readable(sample: Sample) -> bool:
    """Whether this sample actually read something from the kernel.

    A sample whose every counter is ``None`` is a heartbeat, not a reading.
    Counting heartbeats as coverage is how a sampler watching a container
    that died five seconds in would still claim to have measured the whole
    turn — the precise failure this distinction exists to prevent.
    """
    return any(
        getattr(sample, field) is not None
        for field in (
            "cpu_usec",
            "mem_bytes",
            "io_read_bytes",
            "io_write_bytes",
            "net_rx_bytes",
            "net_tx_bytes",
        )
    )


def rollup(samples: list[Sample], *, window_s: float) -> dict[str, object]:
    """Turn a window's samples into the numbers one attempt row carries.

    ``coverage`` is the share of the window that readable samples actually
    span. It is the row's honesty: a sampler that bound late, died early, or
    read nothing at all produces a number below 1, and a reader must present
    that as incomplete rather than as a measurement.
    """
    read = [sample for sample in samples if _readable(sample)]
    if len(read) < 2 or window_s <= 0:
        return {
            "sample_count": len(read),
            "coverage": 0.0,
            "sampler_version": SAMPLER_VERSION,
        }
    observed = read[-1].mono - read[0].mono
    memory = [s.mem_bytes for s in read if s.mem_bytes is not None]
    return {
        "cpu_usec": _delta(read, "cpu_usec"),
        # Peak and mean come from our own samples, so both describe this
        # window; the kernel's high-water mark would not.
        "mem_peak_bytes": max(memory) if memory else None,
        "mem_avg_bytes": int(sum(memory) / len(memory)) if memory else None,
        "io_read_bytes": _delta(read, "io_read_bytes"),
        "io_write_bytes": _delta(read, "io_write_bytes"),
        "net_rx_bytes": _delta(read, "net_rx_bytes"),
        "net_tx_bytes": _delta(read, "net_tx_bytes"),
        "sample_count": len(read),
        "coverage": max(0.0, min(1.0, observed / window_s)),
        "sampler_version": SAMPLER_VERSION,
    }


def container_pid(container: str, *, docker_binary: str = "docker") -> int | None:
    """Ask the daemon for the host pid of a running container.

    Returns None on every failure — a container that has not been created
    yet reports nothing, one that has exited reports 0, and a daemon
    answering from inside a VM (macOS, colima) reports a pid this host
    cannot resolve. All three are "not measurable *yet* or at all", which the
    sampler resolves by retrying rather than by giving up at the first ask.
    """
    try:
        result = subprocess.run(
            [docker_binary, "inspect", "--format", "{{.State.Pid}}", container],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        pid = int(result.stdout.strip())
    except ValueError:
        return None
    return pid if pid > 0 else None


class ResourceSampler:
    """Samples one sandbox for the life of one attempt window.

    Binding is **lazy and retried**, because ``docker run`` returns a client
    process long before the daemon has created the container: asking who to
    measure at spawn time reliably asks too early and gets nothing. The
    sampler therefore starts immediately, keeps asking on its own thread
    until the container appears, and records how much of the window it
    managed to observe.

    Lives on a daemon thread so a stuck read can never hold up the turn's
    own exit, and every public method contains its own failures: the sampler
    is an observer, and an observer that can break the thing it observes is
    not worth having.
    """

    #: Bounded so a long turn cannot grow the runner's memory without limit.
    #: At one sample a second this is over three hours of window; older
    #: samples fall off the front, which costs the delta its true origin and
    #: is reported honestly through the shortened coverage span.
    MAX_SAMPLES = 12_000

    def __init__(
        self,
        counters: ContainerCounters | None = None,
        *,
        resolver: Callable[[], ContainerCounters | None] | None = None,
        interval_s: float = SAMPLE_INTERVAL_S,
        bind_timeout_s: float = BIND_TIMEOUT_S,
    ) -> None:
        self._counters = counters
        self._resolver = resolver
        self._interval = interval_s
        self._bind_timeout = bind_timeout_s
        self._samples: list[Sample] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_mono: float | None = None

    @classmethod
    def for_container(
        cls,
        container: str | None,
        *,
        docker_binary: str = "docker",
        proc_root: Path | str = "/proc",
        cgroup_root: Path | str = "/sys/fs/cgroup",
        interval_s: float = SAMPLE_INTERVAL_S,
    ) -> ResourceSampler:
        """Build a sampler that binds to a named container when it appears.

        Resolution is deferred to the sampling thread rather than performed
        here: this is called from the path that launches the agent, where a
        daemon round trip is both too early to succeed and too expensive to
        spend. A host that can never be read (no container, a daemon in a VM,
        cgroup v1) simply never binds, and reports coverage 0 — the true
        statement.
        """
        if not container:
            return cls(None)

        def _resolve() -> ContainerCounters | None:
            pid = container_pid(container, docker_binary=docker_binary)
            if pid is None:
                return None
            counters = ContainerCounters(pid, proc_root=proc_root, cgroup_root=cgroup_root)
            return counters if counters.readable else None

        return cls(resolver=_resolve, interval_s=interval_s)

    @property
    def active(self) -> bool:
        """Whether this sampler has something to measure, or may yet bind."""
        return self._counters is not None or self._resolver is not None

    def start(self) -> None:
        """Begin sampling. Safe to call on an inert sampler."""
        self._started_mono = time.monotonic()
        if not self.active or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="resource-sampler", daemon=True)
        self._thread.start()

    def _bind(self, deadline: float) -> bool:
        """Resolve the container, retrying until it exists or time runs out."""
        if self._counters is not None:
            return True
        if self._resolver is None:
            return False
        while not self._stop.is_set() and time.monotonic() < deadline:
            try:
                counters = self._resolver()
            except Exception:
                counters = None
            if counters is not None:
                self._counters = counters
                return True
            self._stop.wait(self._interval)
        return False

    def _sample_once(self) -> bool:
        """Append one sample; False once the container is gone."""
        counters = self._counters
        if counters is None:
            return False
        try:
            sample = counters.read()
        except Exception:
            return True
        if sample is None:
            return False
        self._samples.append(sample)
        del self._samples[: -self.MAX_SAMPLES]
        return True

    def _loop(self) -> None:
        """Bind, then sample until stopped or until the container exits."""
        if not self._bind(time.monotonic() + self._bind_timeout):
            return
        # An immediate first sample anchors the window at its true start: a
        # delta measured from one tick in would silently omit the busiest
        # second of most turns, and the missing span would read as a partial
        # measurement of a turn that was measured fine.
        while not self._stop.is_set():
            if not self._sample_once():
                return
            self._stop.wait(self._interval)

    def stop(self) -> dict[str, object]:
        """Stop sampling and return the window's rollup.

        A final sample is taken before the thread is joined so the window
        closes where it actually ended rather than at the last tick, which is
        what keeps a healthy turn's coverage at 1 instead of a systematic
        fraction below it.

        The join is bounded: a sampler thread wedged in a slow read must not
        delay the turn's terminal transition.
        """
        window = time.monotonic() - self._started_mono if self._started_mono is not None else 0.0
        self._stop.set()
        with contextlib.suppress(Exception):
            self._sample_once()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2 * self._interval)
        return rollup(list(self._samples), window_s=window)
