"""Reuse a host's runtime probe only while its observed Docker connection lives.

Evidence stays in the trusted runner, crosses a private inherited socket, and
expires within a minute. A Docker ID is not a daemon generation: the original
HTTP connection must still answer, without reconnecting. The installer epoch
also fences Kata changes that do not restart Docker. Mutable network rules are
deliberately outside this evidence and remain a check on every turn.
"""

from __future__ import annotations

import contextlib
import dataclasses
import http.client
import json
import os
import pathlib
import secrets
import socket
import stat
import subprocess
import threading
import time
import uuid
from typing import Any
from urllib.parse import quote

from cloud_agent_host.sandbox import KATA_RUNTIME, ContainerBackend, SandboxBackend
from cloud_agent_shared.errors import SandboxError

RUNTIME_STATE_PATH = "/run/agent-kata-runtime/epoch.json"
QUALIFICATION_TTL_S = 60.0
_HANDOFF_TTL_S = 5.0
_WIRE_TIMEOUT_S = 45.0
_WIRE_LIMIT = 65536
_API_LIMIT = 2 * 1024 * 1024


def _file_identity(observed: os.stat_result) -> list[int]:
    return [
        observed.st_dev,
        observed.st_ino,
        observed.st_size,
        observed.st_mtime_ns,
        observed.st_ctime_ns,
    ]


def _require_safe_mode(observed: os.stat_result, *, directory: bool, root: bool) -> None:
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        not expected(observed.st_mode)
        or observed.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or (root and observed.st_uid != 0)
    ):
        raise SandboxError("runtime qualification marker has unsafe ownership or permissions")


def _read_marker(path: str, *, root: bool) -> tuple[str, list[int]] | None:
    """Read a bounded regular marker without following its file or directory symlink."""
    target = pathlib.Path(path)
    try:
        directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SandboxError("runtime qualification marker directory cannot be trusted") from exc
    try:
        if root:
            _require_safe_mode(os.fstat(directory), directory=True, root=True)
        try:
            descriptor = os.open(
                target.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise SandboxError("runtime qualification marker cannot be trusted") from exc
        with os.fdopen(descriptor, "rb") as source:
            before = os.fstat(source.fileno())
            _require_safe_mode(before, directory=False, root=root)
            raw = source.read(4097)
            after = os.fstat(source.fileno())
            if len(raw) > 4096 or _file_identity(before) != _file_identity(after):
                raise SandboxError("runtime qualification marker changed while being read")
            try:
                return raw.decode("utf-8").strip(), _file_identity(after)
            except UnicodeDecodeError as exc:
                raise SandboxError("runtime qualification marker is not UTF-8") from exc
    except OSError as exc:
        raise SandboxError("runtime qualification marker could not be read") from exc
    finally:
        os.close(directory)


def check_runtime_state(path: str = RUNTIME_STATE_PATH) -> dict[str, Any] | None:
    """Refuse an unsafe or changing runtime; absence permits only a full probe."""
    marker = _read_marker(path, root=True)
    if marker is None:
        return None
    raw, identity = marker
    try:
        state = json.loads(raw)
        if not isinstance(state, dict) or type(state.get("version")) is not int:
            raise ValueError("invalid version")
        if state["version"] != 1 or state.get("state") not in {"ready", "updating"}:
            raise ValueError("invalid state")
        if not isinstance(state.get("generation"), str):
            raise ValueError("invalid generation")
        generation = str(uuid.UUID(state["generation"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise SandboxError("runtime qualification epoch is malformed") from exc
    if state["state"] == "updating":
        raise SandboxError("Kata runtime is being updated; refusing to accept a turn")
    return {"generation": generation, "identity": identity}


def _backend_identity(backend: ContainerBackend) -> dict[str, Any]:
    return {
        "image": backend.image,
        "runtime": backend.runtime,
        "docker_binary": backend.docker_binary,
        "extra_args": list(backend.extra_args),
        "uid": backend.uid,
        "gid": backend.gid,
        "home": backend.home,
        "namespace": backend.sandbox_namespace,
        "network": json.loads(json.dumps(dataclasses.asdict(backend.sandbox_network), default=str))
        if backend.sandbox_network is not None
        else None,
    }


def _local_identity(
    backend: ContainerBackend, deployment_sha: str, readiness_path: str, runtime_state_path: str
) -> dict[str, Any] | None:
    runtime = check_runtime_state(runtime_state_path)
    if runtime is None or not deployment_sha or not readiness_path:
        return None
    ready = _read_marker(readiness_path, root=False)
    if ready is None or ready[0] != deployment_sha:
        raise SandboxError("runtime qualification deployment is not ready")
    return {
        "backend": _backend_identity(backend),
        "deployment_sha": deployment_sha,
        "ready": [ready[0], ready[1]],
        "runtime_epoch": runtime,
        "host_kernel": os.uname().release,
    }


def _docker_socket(backend: ContainerBackend) -> str | None:
    """Use only a direct local daemon, resolved through the same Docker CLI."""
    if backend.runtime != KATA_RUNTIME or os.environ.get("DOCKER_CONTEXT"):
        return None
    if os.environ.get("DOCKER_TLS_VERIFY") or os.environ.get("DOCKER_CERT_PATH"):
        return None
    endpoint = os.environ.get("DOCKER_HOST", "")
    if not endpoint:
        try:
            result = subprocess.run(
                [
                    backend.docker_binary,
                    "context",
                    "inspect",
                    "--format",
                    "{{json .Endpoints.docker.Host}}",
                ],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            endpoint = json.loads(result.stdout) if result.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return None
    if not isinstance(endpoint, str) or not endpoint.startswith("unix://"):
        return None
    path = endpoint.removeprefix("unix://")
    return path if path.startswith("/") and "\x00" not in path else None


class _DaemonConnection(http.client.HTTPConnection):
    """One accepted Docker connection; losing it must never reconnect silently."""

    def __init__(self, path: str) -> None:
        super().__init__("docker", timeout=5)
        self.path = path
        self._connected_once = False
        self._socket_identity: list[int] | None = None

    def same_endpoint(self) -> bool:
        """Reject replacement of the socket path even if the old daemon still lives."""
        try:
            observed = os.stat(self.path)
        except OSError as exc:
            raise SandboxError("qualified Docker socket disappeared") from exc
        return stat.S_ISSOCK(observed.st_mode) and _file_identity(observed) == self._socket_identity

    def connect(self) -> None:
        """Connect once, then force the authority to requalify after any loss."""
        if self._connected_once:
            raise SandboxError("qualified Docker connection was lost")
        self._connected_once = True
        connected = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            observed = os.stat(self.path)
            if not stat.S_ISSOCK(observed.st_mode):
                raise SandboxError("Docker endpoint is not a Unix socket")
            self._socket_identity = _file_identity(observed)
            connected.settimeout(self.timeout)
            connected.connect(self.path)
            if not self.same_endpoint():
                raise SandboxError("Docker socket changed while connecting")
        except BaseException:
            connected.close()
            raise
        self.sock = connected

    def document(self, path: str) -> dict[str, Any]:
        """Read one bounded document without a retry or replacement connection."""
        try:
            self.request("GET", path)
            response = self.getresponse()
            raw = response.read(_API_LIMIT + 1)
            if response.status != 200 or response.will_close or len(raw) > _API_LIMIT:
                raise SandboxError("qualified Docker connection returned an unusable response")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("not an object")
            return value
        except (OSError, http.client.HTTPException, ValueError) as exc:
            self.close()
            raise SandboxError("qualified Docker connection failed") from exc


def _daemon_identity(connection: _DaemonConnection, backend: ContainerBackend) -> dict[str, Any]:
    info = connection.document("/info")
    image = connection.document(f"/images/{quote(backend.image, safe='')}/json")
    if not connection.same_endpoint():
        raise SandboxError("Docker socket changed while reading qualification state")
    image_id = image.get("Id")
    if not isinstance(image_id, str) or not image_id.startswith("sha256:"):
        raise SandboxError("runtime qualification could not resolve the sandbox image")
    if not isinstance(info.get("Runtimes"), dict) or not info.get("KernelVersion"):
        raise SandboxError("runtime qualification received incomplete Docker configuration")
    return {
        "image_id": image_id,
        "daemon": {
            key: info.get(key)
            for key in (
                "ID",
                "ServerVersion",
                "KernelVersion",
                "OSType",
                "Architecture",
                "DefaultRuntime",
                "Runtimes",
                "SecurityOptions",
                "Driver",
            )
        },
    }


def _wire_timeout(channel: socket.socket, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SandboxError("runtime qualification channel timed out")
    channel.settimeout(remaining)


def _receive(channel: socket.socket, *, deadline: float | None = None) -> dict[str, Any]:
    deadline = time.monotonic() + _WIRE_TIMEOUT_S if deadline is None else deadline
    chunks = bytearray()
    while len(chunks) <= _WIRE_LIMIT:
        _wire_timeout(channel, deadline)
        part = channel.recv(min(4096, _WIRE_LIMIT + 1 - len(chunks)))
        if not part:
            raise SandboxError("runtime qualification channel closed without an answer")
        chunks.extend(part)
        if b"\n" in part:
            line, _, trailing = chunks.partition(b"\n")
            if trailing:
                raise SandboxError("runtime qualification channel sent trailing data")
            try:
                value = json.loads(line)
            except (ValueError, UnicodeDecodeError) as exc:
                raise SandboxError("runtime qualification channel sent invalid JSON") from exc
            if not isinstance(value, dict):
                raise SandboxError("runtime qualification channel sent an invalid message")
            return value
    raise SandboxError("runtime qualification message is too large")


def _send(
    channel: socket.socket, message: dict[str, Any], *, deadline: float | None = None
) -> None:
    raw = json.dumps(message, separators=(",", ":")).encode() + b"\n"
    if len(raw) > _WIRE_LIMIT:
        raise SandboxError("runtime qualification message is too large")
    _wire_timeout(channel, time.monotonic() + _WIRE_TIMEOUT_S if deadline is None else deadline)
    channel.sendall(raw)


class HostQualification:
    """Refresh host evidence before claims and attest it over a private socket."""

    def __init__(
        self,
        backend: ContainerBackend,
        *,
        deployment_sha: str,
        readiness_path: str,
        runtime_state_path: str = RUNTIME_STATE_PATH,
    ) -> None:
        self.backend = backend
        self.deployment_sha = deployment_sha
        self.readiness_path = readiness_path
        self.runtime_state_path = runtime_state_path
        self._connection: _DaemonConnection | None = None
        self._evidence: dict[str, Any] | None = None
        self._lock = threading.Lock()

    def _local(self) -> dict[str, Any] | None:
        if self.backend.runtime != KATA_RUNTIME:
            return None
        return _local_identity(
            self.backend, self.deployment_sha, self.readiness_path, self.runtime_state_path
        )

    def _invalidate(self) -> None:
        if self._connection is not None:
            self._connection.close()
        self._connection = None
        self._evidence = None

    def close(self) -> None:
        """Forget all evidence and release the original daemon connection."""
        with self._lock:
            self._invalidate()

    def _current(self, local: dict[str, Any], path: str) -> bool:
        if self._connection is None or self._evidence is None:
            return False
        return (
            self._connection.path == path
            and self._connection.same_endpoint()
            and time.monotonic() < self._evidence["expires_mono"]
            and local == self._evidence["local"]
            and _daemon_identity(self._connection, self.backend) == self._evidence["daemon"]
        )

    def refresh(self) -> bool:
        """Qualify before claiming; unsupported transport/absent epoch uses full probes."""
        with self._lock:
            try:
                local = self._local()
                path = _docker_socket(self.backend)
                if local is None or path is None:
                    self._invalidate()
                    return False
                try:
                    if self._current(local, path):
                        return True
                except SandboxError:
                    # A replacement daemon may be healthy. A fresh connection
                    # can prove it, but only by repeating the complete probe.
                    self._invalidate()
                self._invalidate()
                connection = _DaemonConnection(path)
                self._connection = connection
                before = _daemon_identity(connection, self.backend)
                self.backend.preflight(check_network=False)
                after = _daemon_identity(connection, self.backend)
                if before != after or local != self._local():
                    raise SandboxError("runtime changed while it was being qualified")
                guest_kernel = self.backend.validate_guest_kernel(
                    self.backend.version_stamp().get("guest_kernel")
                )
                self.backend._version_stamp = None
                self._evidence = {
                    "local": local,
                    "daemon": after,
                    "guest_kernel": guest_kernel,
                    "expires_mono": time.monotonic() + QUALIFICATION_TTL_S,
                }
                return True
            except BaseException:
                self._invalidate()
                raise

    def serve(self, channel: socket.socket) -> None:
        """Answer one private request; never run a slow probe during handover."""
        deadline = time.monotonic() + _WIRE_TIMEOUT_S
        try:
            request = _receive(channel, deadline=deadline)
            nonce = request.get("nonce")
            if (
                type(request.get("version")) is not int
                or request["version"] != 1
                or not isinstance(nonce, str)
                or len(nonce) != 32
            ):
                raise SandboxError("invalid runtime qualification request")
            with self._lock:
                local = self._local()
                path = _docker_socket(self.backend)
                if local is None or path is None or not self._current(local, path):
                    self._invalidate()
                    answer: dict[str, Any] = {"qualified": False}
                else:
                    # Narrow the transferable lifetime even if the host's
                    # qualification has most of its minute left.
                    answer = {
                        "qualified": True,
                        **(self._evidence or {}),
                        "issued_mono": time.monotonic(),
                    }
            _send(channel, {"version": 1, "nonce": nonce, **answer}, deadline=deadline)
        except Exception as exc:
            self.close()
            with contextlib.suppress(OSError, SandboxError):
                _send(channel, {"version": 1, "error": str(exc)[:300]}, deadline=deadline)


def use_qualification(
    backend: SandboxBackend,
    channel: socket.socket,
    *,
    deployment_sha: str,
    readiness_path: str,
    runtime_state_path: str = RUNTIME_STATE_PATH,
) -> bool:
    """Use only a fresh answer on the parent's inherited socket, or require a probe."""
    nonce = secrets.token_hex(16)
    deadline = time.monotonic() + _WIRE_TIMEOUT_S
    try:
        _send(channel, {"version": 1, "nonce": nonce}, deadline=deadline)
        answer = _receive(channel, deadline=deadline)
    except (OSError, SandboxError) as exc:
        raise SandboxError("runtime qualification could not be received") from exc
    if "error" in answer:
        raise SandboxError("runtime qualification refused: " + str(answer["error"])[:300])
    if (
        type(answer.get("version")) is not int
        or answer["version"] != 1
        or answer.get("nonce") != nonce
    ):
        raise SandboxError("runtime qualification response does not match its request")
    if not isinstance(backend, ContainerBackend) or backend.runtime != KATA_RUNTIME:
        return False
    local = _local_identity(backend, deployment_sha, readiness_path, runtime_state_path)
    if local is None:
        return False
    if answer.get("qualified") is False:
        return False
    issued, expires = answer.get("issued_mono"), answer.get("expires_mono")
    now = time.monotonic()
    if (
        not isinstance(issued, (int, float))
        or isinstance(issued, bool)
        or not isinstance(expires, (int, float))
        or isinstance(expires, bool)
    ):
        raise SandboxError("runtime qualification has invalid expiry")
    if (
        answer.get("qualified") is not True
        or not issued <= now < min(expires, issued + _HANDOFF_TTL_S)
        or expires > issued + QUALIFICATION_TTL_S
        or answer.get("local") != local
        or local != _local_identity(backend, deployment_sha, readiness_path, runtime_state_path)
    ):
        raise SandboxError("runtime qualification is stale or belongs to another configuration")
    guest = backend.validate_guest_kernel(answer.get("guest_kernel"))
    backend._kernel_probe = {"host_kernel": os.uname().release, "guest_kernel": guest}
    backend._version_stamp = None
    return True
