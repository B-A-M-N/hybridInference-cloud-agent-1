"""The member-window reducer: every way one turn's work could be counted twice.

The reducer's whole job is a negative one. It must never charge a thread's
life to two windows, never add a settled exit to the readings that already
covered the same range, never treat a counter that fell as negative work, and
never let an unreadable cycle become a baseline of zero. Those are the cases
below, because a plausible-looking number produced by any of them is worse
than a missing one: a dashboard cannot tell that a total was double counted.
"""

from __future__ import annotations

import json

import pytest

from cloud_agent_host.member_resources import (
    EXIT_COUNTER_CPU,
    EXIT_COUNTER_IO,
    MEMBER_WINDOW_SOURCE,
    STATUS_AVAILABLE,
    STATUS_PARTIAL,
    STATUS_UNAVAILABLE,
    WINDOW_ADDRESS_SPACES_DEDUPED,
    WINDOW_CPU,
    WINDOW_PSS,
    WINDOW_PSS_COMPLETE,
    WINDOW_READING_COMPLETE,
    WINDOW_STORAGE_IO,
    MemberWindowReducer,
    ThreadExit,
    ThreadReading,
    WindowSample,
    exit_events_from_processes,
    instance_key,
    parse_window_sample,
    parse_window_samples,
)

MEASUREMENT = "00000000-0000-4000-8000-000000000001"
BOOT = "b0b1e2d3-0000-0000-0000-000000000000"
COMPLETE = WINDOW_CPU | WINDOW_STORAGE_IO | WINDOW_READING_COMPLETE
COMPLETE_PSS = WINDOW_PSS | WINDOW_PSS_COMPLETE | WINDOW_ADDRESS_SPACES_DEDUPED


def reading(
    tid: int,
    *,
    starttime: int = 100,
    cpu_ns: int | None = 0,
    read_bytes: int | None = 0,
    write_bytes: int | None = 0,
    uid: int = 11001,
) -> ThreadReading:
    """One live task's cumulative readings at one census."""
    return ThreadReading(
        instance=instance_key(boot_id=BOOT, starttime_ticks=starttime, tid=tid),
        tid=tid,
        tgid=tid,
        uid=uid,
        cpu_ns=cpu_ns,
        read_bytes=read_bytes,
        write_bytes=write_bytes,
    )


def cycle(
    seq: int,
    *readings: ThreadReading,
    counters: int = COMPLETE | COMPLETE_PSS,
    mono_ns: int | None = None,
    pss_bytes: int | None = 0,
    address_spaces: int = 1,
    pss_failed: int = 0,
) -> WindowSample:
    """One census cycle, with only the fields under test varied."""
    return WindowSample(
        sample_seq=seq,
        guest_mono_ns=mono_ns if mono_ns is not None else seq * 1_000_000_000,
        counters=counters,
        tasks=readings,
        address_spaces=address_spaces,
        pss_bytes=pss_bytes,
        pss_failed=pss_failed,
    )


def exit_of(
    tid: int,
    *,
    starttime: int = 100,
    cpu_ns: int | None = 0,
    read_bytes: int | None = 0,
    write_bytes: int | None = 0,
    mono_ns: int = 1_000_000_000,
    counter_bits: int = EXIT_COUNTER_CPU | EXIT_COUNTER_IO,
) -> ThreadExit:
    """One task's settled exit, as the kernel reported it."""
    return ThreadExit(
        instance=instance_key(boot_id=BOOT, starttime_ticks=starttime, tid=tid),
        guest_mono_ns=mono_ns,
        cpu_ns=cpu_ns,
        read_bytes=read_bytes,
        write_bytes=write_bytes,
        counter_bits=counter_bits,
    )


def reducer(**kwargs: object) -> MemberWindowReducer:
    return MemberWindowReducer(uid=11001, measurement_id=MEASUREMENT, **kwargs)  # type: ignore[arg-type]


def opened(**kwargs: object) -> MemberWindowReducer:
    """A reducer whose window opened at the guest clock's origin."""
    return reducer(opened_guest_mono_ns=0, **kwargs)


def summary(window: MemberWindowReducer, *, state: str = "final", duration_ms: int = 130_000):
    return window.summary(state=state, duration_ms=duration_ms)


class TestLiveEstimate:
    """A cumulative reading is charged by its growth, never by its value."""

    def test_only_growth_is_charged(self) -> None:
        """Three readings of one instance are one increment, not three totals."""
        window = reducer()
        window.observe_sample(cycle(0, reading(7, cpu_ns=1_000_000_000, read_bytes=100)))
        window.observe_sample(cycle(1, reading(7, cpu_ns=1_500_000_000, read_bytes=160)))
        window.observe_sample(cycle(2, reading(7, cpu_ns=2_000_000_000, read_bytes=200)))

        result = summary(window)

        assert result["cpu"]["usec"] == 1_000_000
        assert result["storage_io"]["read_bytes"] == 100

    def test_the_first_reading_opens_the_range(self) -> None:
        """A resident agent is not billed for everything it ever did."""
        window = reducer()
        window.observe_sample(cycle(0, reading(7, cpu_ns=90_000_000_000)))
        window.observe_sample(cycle(1, reading(7, cpu_ns=90_500_000_000)))

        assert summary(window)["cpu"]["usec"] == 500_000

    def test_two_resident_threads_are_two_ranges(self) -> None:
        window = reducer()
        window.observe_sample(cycle(0, reading(7, cpu_ns=1_000_000_000), reading(8, cpu_ns=2_000)))
        window.observe_sample(cycle(1, reading(7, cpu_ns=1_400_000_000), reading(8, cpu_ns=5_000)))

        assert summary(window)["cpu"]["usec"] == 400_003

    def test_a_new_instance_does_not_donate_its_history_to_the_window(self) -> None:
        """A child that already ran for nine seconds is not nine seconds of this window."""
        window = reducer()
        window.observe_sample(cycle(0, reading(7, cpu_ns=1_000_000_000)))
        window.observe_sample(
            cycle(
                1,
                reading(7, cpu_ns=1_100_000_000),
                reading(9, starttime=500, cpu_ns=9_000_000_000),
            )
        )
        window.observe_sample(
            cycle(
                2,
                reading(7, cpu_ns=1_200_000_000),
                reading(9, starttime=500, cpu_ns=9_100_000_000),
            )
        )

        result = summary(window)

        # 0.2 s of resident work plus 0.1 s of the child's, not 9.3 s.
        assert result["cpu"]["usec"] == 300_000

    def test_an_instance_that_disappears_is_not_negative_work(self) -> None:
        """The member's live set shrinks when a task exits; that is not credit."""
        window = reducer()
        window.observe_sample(cycle(0, reading(7, cpu_ns=5_000_000_000), reading(9, cpu_ns=1_000)))
        window.observe_sample(cycle(1, reading(7, cpu_ns=6_000_000_000), reading(9, cpu_ns=2_000)))
        window.observe_sample(cycle(2, reading(7, cpu_ns=6_500_000_000)))

        result = summary(window)

        assert result["cpu"]["usec"] == 1_500_001

    def test_an_incomplete_cycle_cannot_move_a_total(self) -> None:
        """A cycle that could not read everything may not open or close a range."""
        window = reducer()
        window.observe_sample(cycle(0, reading(7, cpu_ns=1_000_000_000)))
        window.observe_sample(cycle(1, reading(7, cpu_ns=9_000_000_000), counters=WINDOW_CPU))

        result = summary(window)

        assert result["cpu"]["usec"] == 0
        assert result["cpu"]["status"] == STATUS_PARTIAL
        assert "sampling_over_budget" in result["cpu"]["reason_codes"]

    def test_a_counter_that_falls_is_refused_rather_than_reconciled(self) -> None:
        """Reconciling a backwards counter would mean inventing time."""
        window = reducer()
        window.observe_sample(cycle(0, reading(7, cpu_ns=5_000_000_000)))
        window.observe_sample(cycle(1, reading(7, cpu_ns=1_000_000_000)))

        result = summary(window)

        assert result["cpu"]["status"] == STATUS_PARTIAL
        assert "counter_regressed" in result["cpu"]["reason_codes"]
        assert window.regressions == 1


class TestExitEstimate:
    """A settled exit is the kernel's final word for exactly one instance."""

    def test_an_exit_is_counted_once_even_when_delivered_twice(self) -> None:
        window = reducer()
        settled = exit_of(42, cpu_ns=250_000_000, read_bytes=10, write_bytes=2)
        window.observe_exit(settled)
        window.observe_exit(settled)

        result = summary(window)

        assert result["cpu"]["usec"] == 250_000
        assert result["storage_io"]["read_bytes"] == 10
        assert window.duplicate_exits == 1

    def test_a_short_lived_process_is_measured_without_any_census(self) -> None:
        """The sub-second case a one-second sampler would otherwise lose."""
        window = reducer()
        window.observe_exit(exit_of(99, starttime=7, cpu_ns=400_000))

        result = summary(window)

        assert result["cpu"]["usec"] == 400
        assert result["cpu"]["status"] == STATUS_AVAILABLE

    def test_a_missing_counter_is_absent_rather_than_zero(self) -> None:
        """A kernel without task I/O accounting reports nothing, not nothing used."""
        window = reducer()
        window.observe_exit(exit_of(99, cpu_ns=400_000, counter_bits=EXIT_COUNTER_CPU))

        result = summary(window)

        assert result["cpu"]["usec"] == 400
        assert result["storage_io"]["status"] == STATUS_UNAVAILABLE
        assert result["storage_io"]["read_bytes"] is None

    def test_a_recycled_tid_is_a_different_instance(self) -> None:
        """Two threads that shared a TID at different times are two threads."""
        window = reducer()
        window.observe_exit(exit_of(42, starttime=100, cpu_ns=100))
        window.observe_exit(exit_of(42, starttime=200, cpu_ns=300))

        assert summary(window)["cpu"]["usec"] == 0

    def test_ring_loss_marks_the_metric_partial(self) -> None:
        window = reducer()
        window.observe_exit(exit_of(99, cpu_ns=400_000))
        window.note_ring_loss(3)

        result = summary(window)

        assert result["cpu"]["status"] == STATUS_PARTIAL
        assert "ring_loss" in result["cpu"]["reason_codes"]


class TestBothSources:
    """The same work seen twice must not be added twice."""

    def test_a_process_that_ran_and_exited_is_charged_once(self) -> None:
        """The decisive case: readings then a settled exit for one process.

        The census saw the process while it ran; the kernel then reported its
        settled total. Adding those two numbers would bill the process twice,
        and the result would still look entirely plausible.
        """
        window = reducer()
        window.observe_sample(cycle(0, reading(99, cpu_ns=1_000_000_000)))
        window.observe_sample(cycle(1, reading(99, cpu_ns=1_800_000_000)))
        window.observe_exit(exit_of(99, cpu_ns=1_200_000_000, mono_ns=2_500_000_000))

        result = summary(window)

        # The readings took this instance to 1.8 s; the kernel settled 1.2 s.
        # Nothing more is owed, and the double counted 3.0 s must not appear.
        assert result["cpu"]["usec"] == 800_000
        assert result["cpu"]["usec"] < 1_200_000

    def test_the_exit_tail_beyond_the_last_reading_is_added_once(self) -> None:
        """Between the last census and the exit the process may still work."""
        window = reducer()
        window.observe_sample(cycle(0, reading(99, cpu_ns=1_000_000_000)))
        window.observe_sample(cycle(1, reading(99, cpu_ns=1_100_000_000)))
        window.observe_exit(exit_of(99, cpu_ns=1_500_000_000, mono_ns=2_500_000_000))

        result = summary(window)

        assert result["cpu"]["usec"] == 500_000
        assert result["cpu"]["status"] == STATUS_AVAILABLE

    def test_storage_io_follows_the_same_rule(self) -> None:
        window = reducer()
        window.observe_sample(cycle(0, reading(99, read_bytes=1_000, write_bytes=10)))
        window.observe_sample(cycle(1, reading(99, read_bytes=2_000, write_bytes=20)))
        window.observe_exit(exit_of(99, read_bytes=2_600, write_bytes=35, mono_ns=2_500_000_000))

        result = summary(window)

        assert result["storage_io"]["read_bytes"] == 1_600
        assert result["storage_io"]["write_bytes"] == 25

    def test_a_resident_agent_plus_a_short_lived_tool_child(self) -> None:
        """The ordinary shape of a turn."""
        window = reducer()
        window.observe_sample(cycle(0, reading(7, cpu_ns=1_000_000_000)))
        window.observe_sample(cycle(1, reading(7, cpu_ns=2_000_000_000)))
        window.observe_exit(exit_of(9, starttime=500, cpu_ns=300_000, mono_ns=1_500_000_000))
        window.observe_sample(cycle(2, reading(7, cpu_ns=3_000_000_000)))

        result = summary(window)

        assert result["cpu"]["usec"] == 2_000_300


class TestMemory:
    """The peak is a maximum over complete cycles, and nothing else."""

    def test_staggered_peaks_are_compared_not_added(self) -> None:
        """Two processes that each held 1 GiB at different times never held 2."""
        window = reducer()
        window.observe_sample(cycle(0, reading(7), pss_bytes=1_073_741_824))
        window.observe_sample(cycle(1, reading(8, starttime=500), pss_bytes=1_073_741_824))

        assert summary(window)["memory"]["peak_bytes"] == 1_073_741_824

    def test_an_incomplete_cycle_cannot_set_the_peak(self) -> None:
        window = reducer()
        window.observe_sample(cycle(0, reading(7), pss_bytes=1_000))
        window.observe_sample(
            cycle(1, reading(7), pss_bytes=999_999, counters=COMPLETE | WINDOW_PSS, pss_failed=1)
        )

        result = summary(window)

        assert result["memory"]["peak_bytes"] == 1_000
        assert result["memory"]["status"] == STATUS_PARTIAL
        assert result["memory"]["valid_samples"] == 1
        assert result["memory"]["expected_samples"] == 2

    def test_no_complete_cycle_means_no_peak(self) -> None:
        """A missing measurement is not a small one, and RSS is never a substitute."""
        window = reducer()
        window.observe_sample(cycle(0, reading(7), pss_bytes=None, counters=COMPLETE, pss_failed=2))

        result = summary(window)

        assert result["memory"]["peak_bytes"] is None
        assert result["memory"]["status"] == STATUS_UNAVAILABLE

    def test_a_genuine_zero_is_a_measurement(self) -> None:
        window = reducer()
        window.observe_sample(cycle(0, reading(7), pss_bytes=0))

        result = summary(window)

        assert result["memory"]["peak_bytes"] == 0
        assert result["memory"]["status"] == STATUS_AVAILABLE

    def test_unproven_address_space_sharing_degrades_the_metric(self) -> None:
        """Without a dedup proof the sum may count one mapping twice."""
        window = reducer()
        window.observe_sample(
            cycle(0, reading(7), pss_bytes=1_000, counters=WINDOW_PSS | WINDOW_PSS_COMPLETE)
        )

        result = summary(window)

        assert result["memory"]["status"] == STATUS_PARTIAL
        assert "address_space_share_unverified" in result["memory"]["reason_codes"]

    def test_the_gap_between_valid_cycles_is_recorded(self) -> None:
        window = reducer()
        window.observe_sample(cycle(0, reading(7), pss_bytes=1_000, mono_ns=1_000_000_000))
        window.observe_sample(cycle(1, reading(7), pss_bytes=1_000, mono_ns=5_000_000_000))

        assert summary(window)["memory"]["max_gap_ms"] == 4_000

    def test_a_missing_cycle_makes_the_plan_short(self) -> None:
        """A gap in the requester's own counter is a cycle that never ran."""
        window = reducer()
        window.observe_sample(cycle(0, reading(7)))
        window.observe_sample(cycle(4, reading(7)))

        result = summary(window)

        assert result["memory"]["expected_samples"] == 5
        assert "sampling_over_budget" in result["memory"]["reason_codes"]


class TestWindowShape:
    """What the snapshot says about the window, not about the numbers."""

    def test_a_prefix_is_never_rounded_up(self) -> None:
        window = opened()
        window.observe_sample(cycle(0, reading(7), mono_ns=0))
        window.observe_sample(cycle(1, reading(7), mono_ns=12_000_000_000))

        result = summary(window, state="collecting", duration_ms=130_000)

        assert result["window"]["observed_through_ms"] == 12_000
        assert result["window"]["duration_ms"] == 130_000
        assert result["state"] == "collecting"

    def test_a_final_summary_never_claims_past_what_it_saw(self) -> None:
        window = opened()
        window.observe_sample(cycle(0, reading(7), mono_ns=0))
        window.observe_sample(cycle(1, reading(7), mono_ns=12_000_000_000))

        result = summary(window, state="final", duration_ms=130_000)

        assert result["window"]["duration_ms"] == 12_000

    def test_the_scope_and_source_are_stamped(self) -> None:
        result = summary(reducer())

        assert result["scope"] == "member_execution_window"
        assert result["source"] == MEMBER_WINDOW_SOURCE
        assert result["schema_version"] == 1

    def test_no_readings_at_all_is_unavailable_not_zero(self) -> None:
        result = summary(reducer())

        assert result["cpu"]["usec"] is None
        assert result["cpu"]["status"] == STATUS_UNAVAILABLE
        assert result["memory"]["peak_bytes"] is None
        assert result["storage_io"]["read_bytes"] is None

    def test_a_proven_zero_is_zero(self) -> None:
        """A complete window in which the member genuinely used nothing."""
        window = reducer()
        window.observe_sample(cycle(0, reading(7), pss_bytes=0))

        result = summary(window)

        assert result["cpu"]["usec"] == 0
        assert result["cpu"]["status"] == STATUS_AVAILABLE
        assert result["storage_io"]["read_bytes"] == 0

    def test_an_empty_member_is_a_measured_zero_not_a_missing_reading(self) -> None:
        """No tasks at all is the one case where a total of zero is complete."""
        window = reducer()
        window.observe_sample(cycle(0))

        result = summary(window)

        assert result["cpu"]["usec"] == 0
        assert result["cpu"]["status"] == STATUS_AVAILABLE


class TestJournalParsing:
    """Reading the guest's records without inventing what is not there."""

    def record(self, **overrides: object) -> dict[str, object]:
        window: dict[str, object] = {
            "sample_seq": 3,
            "counters": COMPLETE | COMPLETE_PSS,
            "tasks": 1,
            "address_spaces": 1,
            "pss_bytes": 4096,
            "pss_failed": 0,
            "cpu_ns": 5_000_000_000,
            "read_bytes": 100,
            "write_bytes": 20,
            "task_detail": [
                {
                    "tid": 7,
                    "tgid": 7,
                    "uid": 11001,
                    "starttime": 100,
                    "cpu_ns": 5_000_000_000,
                    "read_bytes": 100,
                    "write_bytes": 20,
                    "counters": WINDOW_CPU | WINDOW_STORAGE_IO,
                }
            ],
        }
        window.update(overrides)
        return {
            "version": 2,
            "seq": 11,
            "kind": "window_sample",
            "mono_ns": 1_000_000,
            "measurement_id": MEASUREMENT,
            "uid": 11001,
            "window": window,
        }

    def test_a_record_round_trips(self) -> None:
        parsed = parse_window_sample(self.record(), boot_id=BOOT)

        assert parsed is not None
        assert parsed.sample_seq == 3
        assert parsed.pss_bytes == 4096
        assert len(parsed.tasks) == 1
        assert parsed.tasks[0].tid == 7
        assert parsed.tasks[0].cpu_ns == 5_000_000_000

    def test_another_events_kind_is_not_a_cycle(self) -> None:
        other = self.record()
        other["kind"] = "exit"

        assert parse_window_sample(other, boot_id=BOOT) is None

    @pytest.mark.parametrize(
        "override",
        [
            pytest.param({"sample_seq": None}, id="no-sequence"),
            pytest.param({"counters": None}, id="no-counter-mask"),
        ],
    )
    def test_a_record_missing_its_own_identity_is_skipped(
        self, override: dict[str, object]
    ) -> None:
        assert parse_window_sample(self.record(**override), boot_id=BOOT) is None

    def test_a_task_entry_without_a_start_time_is_skipped(self) -> None:
        """Without the kernel's start time there is no stable instance."""
        detail = [{"tid": 7, "tgid": 7, "uid": 11001, "cpu_ns": 5}]

        parsed = parse_window_sample(self.record(task_detail=detail), boot_id=BOOT)

        assert parsed is not None
        assert parsed.tasks == ()

    def test_only_this_measurement_is_read_out_of_a_shared_guest(self) -> None:
        """A fork sibling's cycles share the journal and must not contribute."""
        mine = json.dumps(self.record()).encode()
        theirs = json.dumps(
            {**self.record(sample_seq=0), "measurement_id": "11111111-2222-4333-8444-555555555555"}
        ).encode()

        samples = parse_window_samples(
            theirs + b"\n" + mine + b"\n",
            measurement_id=MEASUREMENT,
            boot_id=BOOT,
        )

        assert [item.sample_seq for item in samples] == [3]

    def test_a_damaged_line_is_skipped_rather_than_repaired(self) -> None:
        payload = b'{"kind": "window_sample"\n' + json.dumps(self.record()).encode() + b"\n"

        assert len(parse_window_samples(payload, measurement_id=MEASUREMENT, boot_id=BOOT)) == 1

    def test_cycles_are_ordered_by_the_requester_s_counter(self) -> None:
        payload = b"\n".join(json.dumps(self.record(sample_seq=seq)).encode() for seq in (2, 0, 1))

        samples = parse_window_samples(payload, measurement_id=MEASUREMENT, boot_id=BOOT)

        assert [item.sample_seq for item in samples] == [0, 1, 2]


class TestExitEventProjection:
    """Turning observed process rows into exit events without guessing."""

    class Row:
        def __init__(self, **kwargs: object) -> None:
            self.uid = 11001
            self.pid = 42
            self.started_guest_mono_ns = 1_000_000_000
            self.exited_guest_mono_ns = 2_000_000_000
            self.observed_exit = True
            self.cpu_usec = 1_500
            self.io_read_bytes = 10
            self.io_write_bytes = 2
            for key, value in kwargs.items():
                setattr(self, key, value)

    def test_a_settled_row_becomes_one_event(self) -> None:
        events = exit_events_from_processes([self.Row()], boot_id=BOOT)

        assert len(events) == 1
        assert events[0].cpu_ns == 1_500_000
        assert events[0].read_bytes == 10

    def test_a_row_that_never_exited_is_not_an_exit(self) -> None:
        """A process still running at close has no settled figure to report."""
        assert exit_events_from_processes([self.Row(observed_exit=False)], boot_id=BOOT) == []

    def test_a_row_without_a_start_time_is_not_keyed_by_pid(self) -> None:
        """Without the kernel's start time there is no stable identity."""
        assert exit_events_from_processes([self.Row(started_guest_mono_ns=0)], boot_id=BOOT) == []


class TestTheGuestWireFormat:
    """The census record the C serializer actually writes must parse.

    The host parser was tested against a hand-written fixture that matched the
    parser instead of matching the guest. That is how a missing closing brace
    survived: every host test passed while the real journal was unparseable.
    This fixture is the serializer's output shape, byte for byte, and the
    closure of the outer object is the part that was wrong.
    """

    def c_record(self, sample_seq: int = 1, *, task: bool = True) -> str:
        """One line exactly as ``journal_window_sample`` builds it."""
        detail = (
            '{"tid":7,"tgid":7,"uid":11001,"starttime":100,"cpu_ns":5000,'
            '"read_bytes":100,"write_bytes":20,"counters":3}'
            if task
            else ""
        )
        return (
            '{"version":2,"seq":11,"kind":"window_sample","mono_ns":1000000,'
            f'"measurement_id":"{MEASUREMENT}","uid":11001,"window":'
            f'{{"sample_seq":{sample_seq},"counters":79,"tasks":1,"address_spaces":1,'
            '"pss_bytes":4096,"pss_failed":0,"cpu_ns":5000,"read_bytes":100,'
            f'"write_bytes":20,"task_detail":[{detail}]}}}}'
        )

    def test_the_serializer_line_is_valid_json(self) -> None:
        """The record closes both objects it opened."""
        parsed = json.loads(self.c_record())

        assert parsed["kind"] == "window_sample"
        assert parsed["window"]["task_detail"][0]["tid"] == 7

    def test_the_host_reads_back_what_the_guest_wrote(self) -> None:
        samples = parse_window_samples(
            self.c_record().encode() + b"\n",
            measurement_id=MEASUREMENT,
            boot_id=BOOT,
        )

        assert len(samples) == 1
        assert samples[0].address_spaces == 1
        assert samples[0].pss_bytes == 4096
        assert len(samples[0].tasks) == 1
        assert samples[0].tasks[0].tid == 7

    def test_all_three_census_phases_parse(self) -> None:
        """Opening, periodic and closing cycles share one shape."""
        payload = b"\n".join(self.c_record(seq).encode() for seq in (0, 1, 2)) + b"\n"

        samples = parse_window_samples(payload, measurement_id=MEASUREMENT, boot_id=BOOT)

        assert [sample.sample_seq for sample in samples] == [0, 1, 2]

    def test_an_empty_member_cycle_parses(self) -> None:
        samples = parse_window_samples(
            self.c_record(task=False).encode() + b"\n",
            measurement_id=MEASUREMENT,
            boot_id=BOOT,
        )

        assert len(samples) == 1
        assert samples[0].tasks == ()
