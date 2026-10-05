"""Runtime reuse follows a living daemon connection, not a reusable daemon ID."""

from __future__ import annotations

import contextlib
import http.server
import json
import os
import socket
import socketserver
import stat
import threading
import time
import uuid
from types import SimpleNamespace

import pytest

from cloud_agent_host import runtime_qualification as qualification
from cloud_agent_host.sandbox import KATA_RUNTIME, ContainerBackend, ProcessBackend, SandboxError


class Daemon(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path):
        self.connections = []
        self.info = {
            "ID": "unchanged-daemon-id",
            "ServerVersion": "28.0",
            "KernelVersion": "host-kernel",
            "Runtimes": {KATA_RUNTIME: {"path": "kata-shim"}},
        }
        self.image = {"Id": "sha256:original-image"}
        self.close_response = False
        self.status = 200
        super().__init__(str(path), Handler)

    def disconnect(self):
        for connection in self.connections:
            with contextlib.suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
                connection.close()
        self.connections.clear()


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.server.connections.append(self.connection)

    def do_GET(self):
        document = self.server.info if self.path == "/info" else self.server.image
        raw = json.dumps(document).encode()
        self.send_response(self.server.status)
        self.send_header("Content-Length", str(len(raw)))
        if self.server.close_response:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):
        pass


@pytest.fixture
def lab(tmp_path, monkeypatch):
    # A macOS checkout is user-owned. Keep all file-type/mode/symlink checks;
    # exercise the production root-ownership check separately below.
    safe_mode = qualification._require_safe_mode
    monkeypatch.setattr(
        qualification,
        "_require_safe_mode",
        lambda observed, *, directory, root: safe_mode(observed, directory=directory, root=False),
    )
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    epoch = runtime / "epoch.json"
    epoch.write_text(json.dumps({"version": 1, "state": "ready", "generation": str(uuid.uuid4())}))
    epoch.chmod(0o644)
    ready = tmp_path / "ready"
    ready.write_text("deployment-a")
    ready.chmod(0o644)
    # macOS Unix socket paths are limited to roughly one hundred bytes.
    path = f"/tmp/ca-qual-{uuid.uuid4().hex[:12]}.sock"
    daemon = Daemon(path)
    thread = threading.Thread(target=daemon.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("DOCKER_HOST", f"unix://{path}")
    for name in ("DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        monkeypatch.delenv(name, raising=False)
    backend = ContainerBackend(image="fixture@sha256:abc", runtime=KATA_RUNTIME)
    probes = []

    def preflight(*, check_network=True):
        assert check_network is False, "mutable network verification remains per turn"
        probes.append("probe")
        backend._kernel_probe = {"host_kernel": os.uname().release, "guest_kernel": "guest-kernel"}

    monkeypatch.setattr(backend, "preflight", preflight)
    monkeypatch.setattr(backend, "version_stamp", lambda: dict(backend._kernel_probe))
    authority = qualification.HostQualification(
        backend,
        deployment_sha="deployment-a",
        readiness_path=str(ready),
        runtime_state_path=str(epoch),
    )
    yield SimpleNamespace(
        backend=backend, daemon=daemon, authority=authority, probes=probes, epoch=epoch, ready=ready
    )
    authority.close()
    daemon.disconnect()
    daemon.shutdown()
    daemon.server_close()
    thread.join(timeout=2)
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)


def _request(lab, *, backend=None):
    parent, child = socket.socketpair()
    thread = threading.Thread(target=lab.authority.serve, args=(parent,))
    thread.start()
    try:
        return qualification.use_qualification(
            backend or lab.backend,
            child,
            deployment_sha="deployment-a",
            readiness_path=str(lab.ready),
            runtime_state_path=str(lab.epoch),
        )
    finally:
        child.close()
        thread.join(timeout=2)
        parent.close()
        assert not thread.is_alive()


def test_one_full_probe_is_reused_only_through_a_private_handoff(lab):
    assert lab.authority.refresh()
    for _ in range(3):
        assert lab.authority.refresh()
        assert _request(lab)
    assert lab.probes == ["probe"]
    assert len(lab.daemon.connections) == 1
    assert lab.backend._kernel_probe["guest_kernel"] == "guest-kernel"


def test_daemon_restart_with_same_id_and_version_requires_a_new_full_probe(lab):
    assert lab.authority.refresh()
    lab.daemon.disconnect()
    assert lab.authority.refresh()
    assert lab.probes == ["probe", "probe"]
    assert _request(lab)


def test_handover_after_daemon_restart_refuses_before_a_new_probe(lab):
    assert lab.authority.refresh()
    lab.daemon.disconnect()
    with pytest.raises(SandboxError, match="refused"):
        _request(lab)
    assert lab.probes == ["probe"], "serve must remain bounded and not boot a probe VM"


def test_replaced_socket_path_cannot_reuse_a_still_living_old_connection(lab):
    assert lab.authority.refresh()
    path = lab.authority._connection.path
    os.unlink(path)
    # The first fake daemon and its established connection still live, but
    # a CLI using the pathname would no longer reach that daemon.
    replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        replacement.bind(path)
        assert _request(lab) is False
        assert lab.probes == ["probe"]
    finally:
        replacement.close()


@pytest.mark.parametrize(
    "change", ["image", "runtime", "epoch", "ready", "configuration", "expiry"]
)
def test_each_environment_change_requires_a_fresh_probe(lab, change):
    assert lab.authority.refresh()
    if change == "image":
        lab.daemon.image["Id"] = "sha256:replacement-image"
    elif change == "runtime":
        lab.daemon.info["Runtimes"][KATA_RUNTIME]["path"] = "new-shim"
    elif change == "epoch":
        lab.epoch.write_text(
            json.dumps({"version": 1, "state": "ready", "generation": str(uuid.uuid4())})
        )
    elif change == "ready":
        lab.ready.write_text("deployment-a")
    elif change == "configuration":
        lab.backend.extra_args.append("--cpus=2")
    else:
        lab.authority._evidence["expires_mono"] = time.monotonic() - 1
    assert lab.authority.refresh()
    assert lab.probes == ["probe", "probe"]


@pytest.mark.parametrize("failure", ["disconnect", "image_change", "epoch_change", "probe_error"])
def test_failed_or_changed_probe_never_leaves_usable_evidence(lab, monkeypatch, failure):
    def preflight(*, check_network):
        lab.backend._kernel_probe["guest_kernel"] = "guest-kernel"
        if failure == "disconnect":
            lab.daemon.disconnect()
        elif failure == "image_change":
            lab.daemon.image["Id"] = "sha256:changed-during-probe"
        elif failure == "epoch_change":
            lab.ready.write_text("pending:deployment-a")
        else:
            raise SandboxError("probe failed")

    monkeypatch.setattr(lab.backend, "preflight", preflight)
    with pytest.raises(SandboxError):
        lab.authority.refresh()
    assert lab.authority._evidence is None
    assert lab.authority._connection is None


def test_server_connection_close_cannot_be_replaced_silently(lab):
    lab.daemon.close_response = True
    with pytest.raises(SandboxError, match="unusable response"):
        lab.authority.refresh()
    assert not lab.probes
    assert lab.authority._evidence is None


def test_missing_epoch_unsupported_daemon_and_non_kata_fall_back(lab, monkeypatch):
    lab.epoch.unlink()
    assert lab.authority.refresh() is False
    assert _request(lab) is False
    assert not lab.probes
    monkeypatch.setenv("DOCKER_HOST", "tcp://elsewhere:2375")
    assert lab.authority.refresh() is False
    assert _request(lab, backend=ProcessBackend(acknowledged_unsafe=True)) is False


def test_runtime_update_blocks_reuse_and_full_probe(lab, monkeypatch):
    lab.epoch.write_text(
        json.dumps({"version": 1, "state": "updating", "generation": str(uuid.uuid4())})
    )
    with pytest.raises(SandboxError, match="being updated"):
        lab.authority.refresh()
    with pytest.raises(SandboxError, match="being updated"):
        _request(lab)
    monkeypatch.setattr(
        qualification,
        "check_runtime_state",
        lambda: (_ for _ in ()).throw(SandboxError("being updated")),
    )
    with pytest.raises(SandboxError, match="being updated"):
        ContainerBackend(image="fixture", runtime=KATA_RUNTIME).preflight()


def test_runtime_update_does_not_block_an_unrelated_runc_backend(lab):
    lab.epoch.write_text(
        json.dumps({"version": 1, "state": "updating", "generation": str(uuid.uuid4())})
    )
    lab.backend.runtime = "runc"
    assert lab.authority.refresh() is False
    assert _request(lab) is False
    assert not lab.probes


@pytest.mark.parametrize(
    "kind",
    ["symlink", "directory_symlink", "world_writable", "group_writable", "fifo", "malformed"],
)
def test_runtime_marker_must_be_safe(lab, kind):
    if kind == "symlink":
        target = lab.epoch.with_name("actual.json")
        lab.epoch.rename(target)
        lab.epoch.symlink_to(target)
    elif kind == "directory_symlink":
        original = lab.epoch.parent
        target = original.with_name("actual-runtime")
        original.rename(target)
        original.symlink_to(target, target_is_directory=True)
    elif kind in {"world_writable", "group_writable"}:
        lab.epoch.chmod(0o666 if kind == "world_writable" else 0o664)
    elif kind == "fifo":
        lab.epoch.unlink()
        os.mkfifo(lab.epoch)
    else:
        lab.epoch.write_text("{}")
    with pytest.raises(SandboxError):
        qualification.check_runtime_state(str(lab.epoch))


def test_runtime_marker_must_be_root_owned():
    observed = SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_uid=1234)
    with pytest.raises(SandboxError, match="ownership"):
        qualification._require_safe_mode(observed, directory=False, root=True)


@pytest.mark.parametrize(
    "field,value",
    [
        ("guest_kernel", None),
        ("guest_kernel", os.uname().release),
        ("issued_mono", -1),
        ("expires_mono", float("inf")),
        ("expires_mono", True),
        ("local", {}),
        ("nonce", "wrong"),
    ],
)
def test_child_rejects_malformed_stale_or_mismatched_evidence(lab, field, value):
    assert lab.authority.refresh()
    parent, child = socket.socketpair()

    def answer():
        request = qualification._receive(parent)
        proof = {
            "version": 1,
            "nonce": request["nonce"],
            "qualified": True,
            **lab.authority._evidence,
            "issued_mono": time.monotonic(),
            field: value,
        }
        qualification._send(parent, proof)

    thread = threading.Thread(target=answer)
    thread.start()
    try:
        with pytest.raises(SandboxError):
            qualification.use_qualification(
                lab.backend,
                child,
                deployment_sha="deployment-a",
                readiness_path=str(lab.ready),
                runtime_state_path=str(lab.epoch),
            )
    finally:
        child.close()
        thread.join(timeout=2)
        parent.close()


def test_expiry_between_refresh_and_handoff_requests_full_child_probe(lab):
    assert lab.authority.refresh()
    lab.authority._evidence["expires_mono"] = time.monotonic() - 1
    assert _request(lab) is False
    assert lab.probes == ["probe"]


def test_partial_messages_cannot_restart_the_handover_deadline(monkeypatch):
    elapsed = [0.0]
    timeouts = []

    def read(_size):
        elapsed[0] += 30
        return b"x"

    channel = SimpleNamespace(settimeout=timeouts.append, recv=read)
    monkeypatch.setattr(qualification.time, "monotonic", lambda: elapsed[0])
    with pytest.raises(SandboxError, match="timed out"):
        qualification._receive(channel)
    assert timeouts == [45, 15]
