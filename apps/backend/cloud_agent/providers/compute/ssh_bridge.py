"""Publish the stable SSH daemon to a compute node that cannot route to it.

Automatic setup records the address the control plane uses for the stable
machine. On this staging network that address is the Docker bridge gateway.
A CloudLab node has a different machine at that address, so its tunnels fail
host-key checks. The control plane can already SSH to the node, so it
reverse-forwards the stable sshd onto the node's loopback. The node relays
that port onto its Docker bridge, which is the address containers can dial.
"""

from __future__ import annotations

import io
import ipaddress
import re
import tarfile
from typing import Any

BRIDGE_PORT = 2222
BRIDGE_LISTEN_HOST = "172.17.0.1"
_TUNNEL_PREFIXES = ("AGENT_TUNNEL", "AGENT_MCP_TUNNEL", "AGENT_CONTROL_PLANE_TUNNEL")
_DESTINATION = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_.-]{0,31})@([A-Za-z0-9.-]+)$")
_KEY_LINE = re.compile(
    r"^(?P<host>\[[^\]]+\](?::\d+)?|[A-Za-z0-9.:-]+)[ \t]+"
    r"(?P<type>ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521))[ \t]+"
    r"(?P<key>[A-Za-z0-9+/]+={0,3})[ \t]*$"
)

# Runs on the node as root. Listens only on Docker bridge addresses.
NODE_PROXY_SOURCE = """import select
import socket
import threading

LISTEN = ["172.17.0.1", "172.18.0.1", "172.19.0.1"]
PORT = 2222
TARGET = ("127.0.0.1", 2222)


def handle(client):
    try:
        upstream = socket.create_connection(TARGET, 10)
    except OSError:
        client.close()
        return
    pair = [client, upstream]
    try:
        while True:
            readable, _, _ = select.select(pair, [], [], 300)
            if not readable:
                break
            for sock in readable:
                data = sock.recv(65536)
                if not data:
                    return
                other = upstream if sock is client else client
                other.sendall(data)
    finally:
        client.close()
        upstream.close()


def serve(ip):
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind((ip, PORT))
    except OSError:
        return
    server.listen(128)
    while True:
        client, _ = server.accept()
        threading.Thread(target=handle, args=(client,), daemon=True).start()


def main():
    for ip in LISTEN:
        threading.Thread(target=serve, args=(ip,), daemon=True).start()
    threading.Event().wait()


if __name__ == "__main__":
    main()
"""


def key_material(text: str) -> set[str]:
    """Return ``type key`` tokens from OpenSSH known_hosts or ssh-keyscan text."""
    found = set()
    for line in text.splitlines():
        match = _KEY_LINE.match(line.strip())
        if match:
            found.add(match.group("type") + " " + match.group("key"))
    return found


def tunnel_targets(environment: str) -> list[tuple[str, str, int]]:
    """Return ``(prefix, host, port)`` for tunnel destinations shaped as user@host."""
    values = _dotenv(environment)
    targets = []
    for prefix in _TUNNEL_PREFIXES:
        destination = values.get(prefix + "_SSH_DESTINATION", "")
        match = _DESTINATION.fullmatch(destination)
        if not match:
            continue
        port_text = values.get(prefix + "_SSH_PORT", "22")
        if not port_text.isdigit():
            continue
        port = int(port_text)
        if 1 <= port <= 65535:
            targets.append((prefix, match.group(2), port))
    return targets


def needs_bridge(host: str, known_hosts: str, scan: str) -> bool:
    """A private dial address that does not present the verified key needs a bridge.

    A hostname that does not match is left alone: it may be a DNS failure, and
    inventing a bridge would hide that. A private address whose scan is empty
    or belongs to another host is the Docker-bridge collision this exists for.
    """
    recorded = key_material(known_hosts)
    if not recorded or recorded & key_material(scan):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private


def matching_endpoint(
    known_hosts: str, scans: dict[tuple[str, int], str]
) -> tuple[str, int] | None:
    """Pick the local sshd whose key is the one the profile already trusts."""
    recorded = key_material(known_hosts)
    for (host, port), scan in scans.items():
        if recorded & key_material(scan):
            return host, port
    return None


def retarget_known_hosts(text: str, *, listen_host: str, port: int) -> str:
    """Keep the verified keys and name them for the bridge listeners."""
    label = listen_host if port == 22 else f"[{listen_host}]:{port}"
    lines = []
    seen = set()
    for token in sorted(key_material(text)):
        if token in seen:
            continue
        seen.add(token)
        lines.append(f"{label} {token}")
    return ("\n".join(lines) + "\n") if lines else ""


def retarget_environment(text: str, *, listen_host: str, port: int) -> str:
    """Point tunnel dial addresses at the bridge. Other lines stay as written."""
    lines = []
    for line in text.splitlines():
        key, separator, raw = line.partition("=")
        if not separator:
            lines.append(line)
            continue
        value, quoted = _unquote(raw)
        prefix = _tunnel_prefix(key)
        if prefix and key.endswith("_SSH_DESTINATION"):
            match = _DESTINATION.fullmatch(value)
            if match:
                value = match.group(1) + "@" + listen_host
        elif prefix and key.endswith("_SSH_PORT"):
            value = str(port)
        lines.append(key + "=" + (_quote(value) if quoted else value))
    suffix = "\n" if text.endswith("\n") or lines else ""
    return "\n".join(lines) + suffix


def retarget_archive(blob: bytes, *, listen_host: str, port: int) -> bytes:
    """Rewrite the deployment archive's env and known_hosts for the bridge."""
    output = io.BytesIO()
    with (
        tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as source,
        tarfile.open(fileobj=output, mode="w:gz") as dest,
    ):
        for member in source.getmembers():
            extracted = source.extractfile(member) if member.isfile() else None
            data = extracted.read() if extracted is not None else None
            name = member.name.removeprefix("./")
            if data is not None and name == ".env":
                data = retarget_environment(
                    data.decode(), listen_host=listen_host, port=port
                ).encode()
                member.size = len(data)
            elif data is not None and name.endswith(".known_hosts"):
                data = retarget_known_hosts(
                    data.decode(), listen_host=listen_host, port=port
                ).encode()
                member.size = len(data)
            dest.addfile(member, io.BytesIO(data) if data is not None else None)
    return output.getvalue()


def archive_texts(blob: bytes) -> tuple[str, str]:
    """Return the archive's environment and the concatenation of its known_hosts."""
    environment = ""
    known = []
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as source:
        for member in source.getmembers():
            if not member.isfile():
                continue
            extracted = source.extractfile(member)
            if extracted is None:
                continue
            name = member.name.removeprefix("./")
            if name == ".env":
                environment = extracted.read().decode()
            elif name.endswith(".known_hosts"):
                known.append(extracted.read().decode())
    return environment, "".join(known)


def _dotenv(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        key, separator, raw = line.partition("=")
        if separator and re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            values[key], _ = _unquote(raw)
    return values


def _unquote(raw: str) -> tuple[str, bool]:
    if len(raw) >= 2 and raw.startswith("'") and raw.endswith("'"):
        return raw[1:-1], True
    return raw, False


def _quote(value: str) -> str:
    return "'" + value + "'"


def _tunnel_prefix(key: str) -> str | None:
    for prefix in _TUNNEL_PREFIXES:
        if key == prefix or key.startswith(prefix + "_"):
            return prefix
    return None


def bridge_listen_host() -> str:
    """The Docker bridge address the deployment archive dials before adjustment."""
    return BRIDGE_LISTEN_HOST


def ssh_bridge_param() -> dict[str, Any]:
    """Parameters the remote recipe reads when it publishes the node-side proxy."""
    return {"port": BRIDGE_PORT, "listen_host": BRIDGE_LISTEN_HOST}
