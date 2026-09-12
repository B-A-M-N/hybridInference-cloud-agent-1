"""The guest's census record, compiled from its real source and parsed back.

Every host test of the window parser used to feed it a hand-written fixture.
That fixture matched the parser rather than the producer, which is how a census
record missing its outer closing brace survived a fully green suite: the host
read zero samples in production while every test passed.

This module closes that gap by extracting ``journal_window_sample`` from
``guest-observer.c`` at test time, compiling it with the host's own compiler,
running it, and handing its bytes to the parser. Nothing here is a copy, so a
change to the serializer's shape is measured rather than assumed.

The extraction is textual on purpose. Including the whole loader would drag in
libbpf, which is not installed on a development machine, and the alternative --
keeping a second copy of the writer in the test tree -- is the very thing that
failed.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import textwrap

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
OBSERVER_C = REPO_ROOT / "deploy" / "sandbox" / "guest-observer.c"
OBSERVER_H = REPO_ROOT / "deploy" / "sandbox" / "guest-observer.h"


def _function_body(source: str, signature: str) -> str:
    """The whole definition whose text starts at ``signature``.

    The serializer's body is full of JSON in string literals, so counting
    braces naively counts ``{`` and ``}`` that are data rather than code and
    loses the end of the function. String, character and comment regions are
    skipped for that reason: the alternative is a harness that reports "the
    source is corrupt" when what actually changed is the record's shape.
    """
    start = source.index(signature)
    brace = source.index("{", start)
    depth = 0
    index = brace
    in_line_comment = False
    in_block_comment = False
    quote = ""
    while index < len(source):
        char = source[index]
        if in_line_comment:
            if char == "\n":
                in_line_comment = False
        elif in_block_comment:
            if char == "*" and source[index + 1 : index + 2] == "/":
                in_block_comment = False
                index += 1
        elif quote:
            if char == "\\":
                index += 1
            elif char == quote:
                quote = ""
        elif char == "/" and source[index + 1 : index + 2] == "/":
            in_line_comment = True
            index += 1
        elif char == "/" and source[index + 1 : index + 2] == "*":
            in_block_comment = True
            index += 1
        elif char in "\"'":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
        index += 1
    raise AssertionError(f"unterminated definition for {signature!r}")


def _compiler() -> str | None:
    return shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")


def build_serializer_harness(tmp_path: pathlib.Path) -> pathlib.Path:
    """Compile the real serializer into a program that prints one record.

    Returns the executable. Raises ``AssertionError`` when the host has no C
    compiler, so a missing toolchain is a visible skip rather than a silently
    weaker test.
    """
    compiler = _compiler()
    if compiler is None:
        raise AssertionError("no C compiler is available to build the guest serializer")

    source = OBSERVER_C.read_text(encoding="utf-8")
    parts = [
        source[source.index("struct window_task {") : source.index("static void handle_signal")],
        _function_body(source, "static int write_all("),
        _function_body(source, "static int format_into("),
        _function_body(source, "static int journal_window_sample("),
    ]
    body = "\n\n".join(parts)

    # The version lives in the shared header, where both the loader and the
    # ABI definition read it from.
    header = OBSERVER_H.read_text(encoding="utf-8")
    version = header[header.index("#define FI_OBSERVER_VERSION") :].split("\n", 1)[0]
    task_capacity = source[source.index("#define MAX_WINDOW_TASKS") :].split("\n", 1)[0]
    line_capacity = source[source.index("#define MAX_WINDOW_LINE_BYTES") :].split("\n", 1)[0]

    harness = textwrap.dedent(
        f"""
        #include <errno.h>
        #include <inttypes.h>
        #include <stdarg.h>
        #include <stdbool.h>
        #include <stdint.h>
        #include <stdio.h>
        #include <string.h>
        #include <unistd.h>

        {version}
        {task_capacity}
        {line_capacity}
        #define MAX_JOURNAL_BYTES (64ULL * 1024ULL * 1024ULL)

        struct observer {{
            int events_fd;
            uint64_t event_seq;
            uint64_t journal_bytes;
        }};

        /* The loader's own monotonic clock, replaced by a constant so the
         * record is byte-for-byte reproducible across runs. */
        static int monotonic_ns(uint64_t *value)
        {{
            *value = 1000000000ULL;
            return 0;
        }}

        /* The extracted helpers may not all be reachable from this harness;
         * that is the harness's shape, not a defect in the source. */
        #pragma GCC diagnostic ignored "-Wunused-function"

        {body}

        int main(void)
        {{
            struct observer observer = {{.events_fd = 1, .event_seq = 0, .journal_bytes = 0}};
            struct window_task tasks[2] = {{
                {{.cpu_ns = 5000, .read_bytes = 100, .write_bytes = 20,
                 .starttime_ticks = 100, .pss_bytes = 4096, .uid = 11001,
                 .tid = 7, .tgid = 7,
                 .counters = (1U << 0) | (1U << 1) | (1U << 2),
                 .pss_counted = true}},
                {{.cpu_ns = 0, .read_bytes = 0, .write_bytes = 0,
                 .starttime_ticks = 0, .pss_bytes = 0, .uid = 11001,
                 .tid = 8, .tgid = 8, .counters = 0, .pss_counted = false}},
            }};
            /* An empty member cycle is a real state and must also serialize. */
            if (journal_window_sample(&observer, 11001, "measurement-1", 0,
                                      (1U << 0) | (1U << 1) | (1U << 2), 1, 4096, 0,
                                      5000, 100, 20, NULL, 0) < 0)
                return 1;
            if (journal_window_sample(&observer, 11001, "measurement-1", 1,
                                      (1U << 6), 2, 8192, 0, 5000, 100, 20,
                                      tasks, 2) < 0)
                return 2;
            return 0;
        }}
        """
    ).strip()

    path = tmp_path / "serializer-harness.c"
    path.write_text(harness, encoding="utf-8")
    binary = tmp_path / "serializer-harness"
    compiled = subprocess.run(
        [compiler, "-O1", "-Wall", "-Wextra", "-Werror", "-o", str(binary), str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if compiled.returncode != 0:
        raise AssertionError(
            "the guest serializer no longer compiles standalone:\n" + compiled.stderr[-2000:]
        )
    return binary


def run_serializer(tmp_path: pathlib.Path) -> bytes:
    """One member window's opening and periodic records, as the guest writes them."""
    binary = build_serializer_harness(tmp_path)
    completed = subprocess.run([str(binary)], capture_output=True, check=False)
    if completed.returncode != 0:
        raise AssertionError(f"the guest serializer failed with exit code {completed.returncode}")
    return completed.stdout
