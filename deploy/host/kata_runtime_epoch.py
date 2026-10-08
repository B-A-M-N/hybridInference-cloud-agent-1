"""Serialize supported Kata updates and publish their host-wide generation.

Only the installer and a networkless deployment helper write this directory.
Runners mount the directory read-only and never treat an updating generation
as qualification evidence. Atomic replacement keeps readers from seeing half
a marker; flock covers installation and concurrent first-time initialization.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import stat
import subprocess
import uuid
from contextlib import contextmanager, suppress
from pathlib import Path

STATE_PARTS = ("var", "lib", "freeinference", "kata-runtime")
LOCK_NAME = ".install.lock"
LOCK_ENV = "_KATA_RUNTIME_EPOCH_LOCK_FD"


def _trusted(info: os.stat_result, *, directory: bool = False, owner: int | None = None) -> None:
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        not expected(info.st_mode)
        or info.st_uid != (os.geteuid() if owner is None else owner)
        or info.st_mode & 0o022
        or (not directory and info.st_nlink != 1)
    ):
        raise RuntimeError("Kata epoch state must be owner-controlled, non-writable by others")


def open_directory(root: Path, *, mounted: bool = False, create: bool = False) -> int | None:
    """Open each fixed component without following symlinks, anchored at root.

    The extract root is the installer's existing test seam. Production uses
    `/`, requires root, and all ancestors belong to root. A helper container
    only sees the dedicated directory at `/run/agent-kata-runtime`.
    """
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    owner = 0 if root == Path("/") else os.geteuid()
    descriptor = os.open(root, flags)
    try:
        _trusted(os.fstat(descriptor), directory=True, owner=owner)
        for name in ("run", "agent-kata-runtime") if mounted else STATE_PARTS:
            if create:
                with suppress(FileExistsError):
                    os.mkdir(name, mode=0o755, dir_fd=descriptor)
            try:
                child = os.open(name, flags, dir_fd=descriptor)
            except FileNotFoundError:
                os.close(descriptor)
                return None
            os.close(descriptor)
            descriptor = child
            _trusted(os.fstat(descriptor), directory=True, owner=owner)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def read_epoch(directory: int) -> dict[str, object] | None:
    try:
        descriptor = os.open(
            "epoch.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as source:
        _trusted(os.fstat(source.fileno()))
        raw = source.read(4097)
    if len(raw) > 4096:
        raise RuntimeError("Kata epoch marker is oversized")
    value = json.loads(raw)
    if (
        not isinstance(value, dict)
        or type(value.get("version")) is not int
        or value["version"] != 1
        or value.get("state") not in {"updating", "ready"}
        or not isinstance(value.get("generation"), str)
        or str(uuid.UUID(value["generation"])) != value["generation"]
    ):
        raise RuntimeError("Kata epoch marker is malformed")
    return value


def write_epoch(directory: int, *, state: str, generation: str) -> None:
    # Refuse symlinks and unsafe existing markers instead of replacing them.
    read_epoch(directory)
    temporary = f".epoch-{uuid.uuid4()}.tmp"
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=directory
    )
    try:
        with os.fdopen(descriptor, "w") as target:
            json.dump({"version": 1, "state": state, "generation": generation}, target)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, "epoch.json", src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory)


@contextmanager
def install_lock(directory: int):
    descriptor = os.open(
        LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory
    )
    try:
        _trusted(os.fstat(descriptor))
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield descriptor
    finally:
        os.close(descriptor)


def inherited_lock(directory: int) -> int:
    """The installer child must hold the exact descriptor passed by its parent."""
    descriptor = int(os.environ.get(LOCK_ENV, "-1"))
    current = os.stat(LOCK_NAME, dir_fd=directory, follow_symlinks=False)
    held = os.fstat(descriptor)
    _trusted(current)
    _trusted(held)
    if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
        raise RuntimeError("Kata install lock does not name the epoch directory")
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return descriptor


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "begin", "initialize", "verify-path"))
    parser.add_argument("--root", type=Path, default=Path("/"))
    parser.add_argument("--mounted", action="store_true")
    args, command = parser.parse_known_args(argv)
    if command[:1] == ["--"]:
        command = command[1:]
    if args.root == Path("/") and os.geteuid() != 0 and args.action != "verify-path":
        parser.error("Kata epoch publication requires root")
    directory = open_directory(args.root, mounted=args.mounted, create=args.action == "install")
    if directory is None:
        if args.action == "verify-path":
            return 0
        parser.error("Kata epoch directory is missing")
    try:
        if args.action == "verify-path":
            return 0
        if args.action == "begin":
            inherited_lock(directory)
            write_epoch(directory, state="updating", generation=str(uuid.uuid4()))
            return 0
        with install_lock(directory) as lock:
            previous = read_epoch(directory)
            if args.action == "initialize":
                if previous is not None:
                    if previous["state"] != "ready":
                        raise RuntimeError("Kata update is incomplete; re-run the installer")
                    return 0
                write_epoch(directory, state="ready", generation=str(uuid.uuid4()))
                return 0
            if not command:
                parser.error("install requires the installer command")
            result = subprocess.run(
                command,
                env={**os.environ, LOCK_ENV: str(lock)},
                pass_fds=(lock,),
                check=False,
            )
            if result.returncode:
                return result.returncode
            current = read_epoch(directory)
            if current is None or current["state"] != "updating":
                raise RuntimeError("installer completed without invalidating the old generation")
            write_epoch(directory, state="ready", generation=str(current["generation"]))
            return 0
    finally:
        os.close(directory)


if __name__ == "__main__":
    raise SystemExit(main())
