"""The SSH bridge retargets only tunnel dial addresses and verified host keys."""

from __future__ import annotations

import io
import tarfile

from cloud_agent.providers.compute.ssh_bridge import (
    BRIDGE_LISTEN_HOST,
    BRIDGE_PORT,
    archive_texts,
    key_material,
    needs_bridge,
    retarget_archive,
    retarget_environment,
    retarget_known_hosts,
    tunnel_targets,
)

ENV = """\
AGENT_TUNNEL_SSH_DESTINATION='ca-i-abc@172.24.0.1'
AGENT_TUNNEL_SSH_PORT='22'
AGENT_MCP_TUNNEL_SSH_DESTINATION='ca-m-abc@172.24.0.1'
AGENT_MCP_TUNNEL_SSH_PORT='22'
AGENT_CONTROL_PLANE_TUNNEL_SSH_DESTINATION='ca-c-abc@172.24.0.1'
AGENT_CONTROL_PLANE_TUNNEL_SSH_PORT='22'
AGENT_BROKER_REVERSE_BIND='172.24.0.1'
AGENT_HOST_SLOTS='3'
"""

KNOWN = """\
# 172.24.0.1:22 SSH-2.0-OpenSSH_9.6
172.24.0.1 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyMaterial0000000000000
172.24.0.1 ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQCExampleRsaKeyMaterial0000
"""

ED25519 = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyMaterial0000000000000"


def test_private_address_with_a_different_key_needs_a_bridge():
    assert needs_bridge(
        "172.24.0.1", KNOWN, "172.24.0.1 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOther\n"
    )
    assert needs_bridge("172.24.0.1", KNOWN, "")
    assert not needs_bridge("172.24.0.1", KNOWN, "172.24.0.1 " + ED25519 + "\n")
    assert not needs_bridge("spark2.example.org", KNOWN, "")


def test_retarget_keeps_broker_bind_and_rewrites_tunnel_dials():
    rewritten = retarget_environment(ENV, listen_host=BRIDGE_LISTEN_HOST, port=BRIDGE_PORT)
    assert tunnel_targets(rewritten) == [
        ("AGENT_TUNNEL", BRIDGE_LISTEN_HOST, BRIDGE_PORT),
        ("AGENT_MCP_TUNNEL", BRIDGE_LISTEN_HOST, BRIDGE_PORT),
        ("AGENT_CONTROL_PLANE_TUNNEL", BRIDGE_LISTEN_HOST, BRIDGE_PORT),
    ]
    assert "AGENT_BROKER_REVERSE_BIND='172.24.0.1'" in rewritten
    assert "AGENT_HOST_SLOTS='3'" in rewritten
    hosts = retarget_known_hosts(KNOWN, listen_host=BRIDGE_LISTEN_HOST, port=BRIDGE_PORT)
    assert f"[{BRIDGE_LISTEN_HOST}]:{BRIDGE_PORT} {ED25519}" in hosts
    assert key_material(hosts) == key_material(KNOWN)
    assert "# 172.24.0.1" not in hosts


def test_archive_rewrite_updates_env_and_known_hosts_only():
    blob = io.BytesIO()
    with tarfile.open(fileobj=blob, mode="w:gz") as archive:
        for name, data in {
            ".env": ENV.encode(),
            ".compute-secrets/gateway.known_hosts": KNOWN.encode(),
            "deploy/host/sandbox_host.sh": b"#!/bin/bash\n",
        }.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    rewritten = retarget_archive(blob.getvalue(), listen_host=BRIDGE_LISTEN_HOST, port=BRIDGE_PORT)
    environment, known = archive_texts(rewritten)
    assert f"@{BRIDGE_LISTEN_HOST}" in environment
    assert f"[{BRIDGE_LISTEN_HOST}]:{BRIDGE_PORT}" in known
    with tarfile.open(fileobj=io.BytesIO(rewritten), mode="r:gz") as archive:
        script = archive.extractfile("deploy/host/sandbox_host.sh")
        assert script is not None
        assert script.read() == b"#!/bin/bash\n"
