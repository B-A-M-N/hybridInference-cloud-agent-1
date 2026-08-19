"""The resource sampler: what it measures, and what it refuses to invent.

The contract these pin is not "the numbers are right" — a unit test cannot
know a real cgroup's truth — but that every *shape* of failure produces an
honest row. A sampler that reported plausible zeros when it could read
nothing would be worse than one that did not exist, because a dashboard
cannot tell a measured zero from a fabricated one.
"""

from __future__ import annotations

import time

from cloud_agent_host.resources import (
    SAMPLER_VERSION,
    ContainerCounters,
    ResourceSampler,
    Sample,
    _parse_io_stat,
    _parse_keyed,
    _parse_net_dev,
    rollup,
)


def _write_container(tmp_path, *, pid: int = 4242, cgroup: str = "/docker/abc"):
    """Lay out a fake procfs/cgroupfs for one container."""
    proc = tmp_path / "proc" / str(pid)
    (proc / "net").mkdir(parents=True)
    (proc / "cgroup").write_text(f"0::{cgroup}\n")
    cg = tmp_path / "cgroup" / cgroup.lstrip("/")
    cg.mkdir(parents=True)
    return proc, cg


def test_parses_cpu_io_and_network_counters():
    """Each backend's format is read as the kernel writes it."""
    assert _parse_keyed("usage_usec 500\nuser_usec 300\n", "usage_usec") == 500
    assert _parse_io_stat("8:0 rbytes=1024 wbytes=2048\n8:16 rbytes=1 wbytes=2\n") == (1025, 2050)
    dev = "Inter-|Receive\n face |bytes\n    lo: 999 0 0 0 0 0 0 0 999 0\n  eth0: 1000 0 0 0 0 0 0 0 2000 0\n"
    assert _parse_net_dev(dev) == (1000, 2000)


def test_loopback_is_not_network_the_platform_carried():
    """Traffic that never left the sandbox must not inflate a user's number."""
    only_lo = "    lo: 5000 0 0 0 0 0 0 0 5000 0\n"
    assert _parse_net_dev(only_lo) == (None, None)


def test_reads_a_container_through_procfs(tmp_path):
    """A readable layout yields a sample with every backend populated."""
    proc, cg = _write_container(tmp_path)
    (cg / "cpu.stat").write_text("usage_usec 1000\n")
    (cg / "memory.current").write_text("2048\n")
    (cg / "io.stat").write_text("8:0 rbytes=10 wbytes=20\n")
    (proc / "net" / "dev").write_text("  eth0: 30 0 0 0 0 0 0 0 40 0\n")

    counters = ContainerCounters(4242, proc_root=tmp_path / "proc", cgroup_root=tmp_path / "cgroup")
    assert counters.readable
    sample = counters.read()
    assert (sample.cpu_usec, sample.mem_bytes) == (1000, 2048)
    assert (sample.io_read_bytes, sample.io_write_bytes) == (10, 20)
    assert (sample.net_rx_bytes, sample.net_tx_bytes) == (30, 40)


def test_unreadable_backend_yields_none_not_zero(tmp_path):
    """A cgroup we cannot see is not a container that used nothing."""
    proc = tmp_path / "proc" / "7"
    (proc / "net").mkdir(parents=True)
    (proc / "cgroup").write_text("0::/nowhere\n")
    (proc / "net" / "dev").write_text("  eth0: 5 0 0 0 0 0 0 0 6 0\n")

    counters = ContainerCounters(7, proc_root=tmp_path / "proc", cgroup_root=tmp_path / "cgroup")
    sample = counters.read()
    assert sample.cpu_usec is None and sample.mem_bytes is None
    # The namespace's own counters are still readable, and still reported.
    assert (sample.net_rx_bytes, sample.net_tx_bytes) == (5, 6)


def test_rollup_reports_deltas_so_a_resident_sandbox_is_not_double_charged():
    """Counters are cumulative; a window owns only its own difference."""
    samples = [
        Sample(mono=0.0, cpu_usec=1_000, net_rx_bytes=500),
        Sample(mono=5.0, cpu_usec=4_000, net_rx_bytes=900),
    ]
    result = rollup(samples, window_s=5.0)
    assert result["cpu_usec"] == 3_000
    assert result["net_rx_bytes"] == 400


def test_peak_memory_comes_from_our_own_samples():
    """The kernel's high-water mark would attribute an earlier turn's spike."""
    samples = [
        Sample(mono=0.0, mem_bytes=100),
        Sample(mono=1.0, mem_bytes=900),
        Sample(mono=2.0, mem_bytes=300),
    ]
    result = rollup(samples, window_s=2.0)
    assert result["mem_peak_bytes"] == 900
    assert result["mem_avg_bytes"] == 433


def test_a_counter_that_went_backwards_reports_nothing():
    """A cgroup replaced mid-window must not surface as a wild number."""
    samples = [Sample(mono=0.0, cpu_usec=9_000), Sample(mono=1.0, cpu_usec=5)]
    assert rollup(samples, window_s=1.0)["cpu_usec"] is None


def test_a_single_sample_cannot_describe_a_window():
    """One reading of a cumulative counter is a lifetime, not a window."""
    result = rollup([Sample(mono=0.0, cpu_usec=1_000)], window_s=5.0)
    assert result["coverage"] == 0.0
    assert "cpu_usec" not in result or result.get("cpu_usec") is None


def test_coverage_reports_the_share_of_the_window_observed():
    """A sampler that died early says so instead of implying a full read."""
    samples = [Sample(mono=0.0, cpu_usec=0), Sample(mono=2.0, cpu_usec=10)]
    assert rollup(samples, window_s=10.0)["coverage"] == 0.2
    assert rollup(samples, window_s=2.0)["coverage"] == 1.0


def test_rollup_always_stamps_the_sampler_that_took_it():
    """A stored row must be readable under the semantics it was written with."""
    assert rollup([], window_s=0.0)["sampler_version"] == SAMPLER_VERSION


def test_an_inert_sampler_starts_stops_and_reports_zero_coverage():
    """The normal outcome on an unmeasurable host, and never an error."""
    sampler = ResourceSampler(None)
    assert not sampler.active
    sampler.start()
    result = sampler.stop()
    assert result["coverage"] == 0.0
    assert result["sample_count"] == 0


def test_a_sampler_without_a_container_is_inert_rather_than_failing():
    """No container name (process backend, unresolvable daemon) measures nothing."""
    assert not ResourceSampler.for_container(None).active


def test_sampling_collects_and_stops_cleanly(tmp_path):
    """The live loop gathers samples and the stop is bounded."""
    proc, cg = _write_container(tmp_path, pid=99)
    (cg / "cpu.stat").write_text("usage_usec 10\n")
    (cg / "memory.current").write_text("64\n")
    (proc / "net" / "dev").write_text("  eth0: 1 0 0 0 0 0 0 0 2 0\n")

    counters = ContainerCounters(99, proc_root=tmp_path / "proc", cgroup_root=tmp_path / "cgroup")
    sampler = ResourceSampler(counters, interval_s=0.01)
    sampler.start()
    time.sleep(0.05)
    result = sampler.stop()
    assert result["sample_count"] >= 2
    assert result["sampler_version"] == SAMPLER_VERSION


def test_a_read_that_raises_never_escapes_the_sampler(tmp_path):
    """An observer that can break its subject is not worth having."""

    class Exploding(ContainerCounters):
        def read(self):
            raise OSError("counter vanished")

    counters = Exploding(1, proc_root=tmp_path, cgroup_root=tmp_path)
    sampler = ResourceSampler(counters, interval_s=0.01)
    sampler.start()
    time.sleep(0.03)
    # No exception reaches the caller, and the window is honestly empty.
    assert sampler.stop()["coverage"] == 0.0
