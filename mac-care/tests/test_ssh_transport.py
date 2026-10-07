"""Real OpenSSH on ephemeral loopback ports; no host account/config edits.

Requires root and an existing OpenSSH/PAM installation on Linux. The isolated
daemon trusts only a throwaway key restricted to a fixed echo fixture. It never
reads or edits the owner's actual SSH files or starts a shell session.
"""
import argparse
import importlib.util
import os
from pathlib import Path
import shutil
import socket
import socketserver
import subprocess
import threading
import time

import pytest

spec = importlib.util.spec_from_file_location(
    "mac_care_transport", Path(__file__).resolve().parents[1] / "scripts" / "mac_care.py")
mc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mc)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.sendall(b"mac-care-fixture-only")


@pytest.mark.skipif(os.geteuid() != 0 or not shutil.which("sshd"),
                    reason="isolated OpenSSH test requires root and sshd")
def test_real_reverse_forward_and_denied_extra_access(tmp_path):
    host_key, tunnel_key = tmp_path / "host_key", tmp_path / "tunnel_key"
    for path in (host_key, tunnel_key):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)],
                       check=True, timeout=10)
    server_port, listen_port, forbidden_port = free_port(), free_port(), free_port()
    relay_pub = tmp_path / "relay.pub"
    relay_pub.write_text(" ".join(Path(str(host_key) + ".pub").read_text().split()[:2]))
    directory = tmp_path / "session"
    mc.prepare(argparse.Namespace(session=directory, relay_host="127.0.0.1",
                                  relay_user="support", relay_key=relay_pub,
                                  relay_port=server_port, listen_port=listen_port, hours=1))
    state = mc.read_json(directory / "session.json")
    mac_key = relay_pub.read_text()
    report = tmp_path / "report"
    outbound = " ".join(Path(str(tunnel_key) + ".pub").read_text().split()[:2])
    report.write_text(f'SESSION={state["id"]}\nUSER=macuser\nHOST_KEY={mac_key}\n'
                      f'HOST_FINGERPRINT={mc.fingerprint(mac_key)}\nTUNNEL_KEY={outbound}\n')
    mc.enroll(argparse.Namespace(report=report, host="127.0.0.1", port=listen_port),
              directory, state)
    # Generated key options are tested exactly, independent of Mac or relay provisioning.
    auth = directory / "relay-entry.txt"
    config = tmp_path / "sshd_config"
    config.write_text(
        f"Port {server_port}\nListenAddress 127.0.0.1\nHostKey {host_key}\n"
        f"PidFile {tmp_path / 'sshd.pid'}\nAuthorizedKeysFile {auth}\n"
        "PasswordAuthentication no\nKbdInteractiveAuthentication no\nUsePAM yes\n"
        "PermitRootLogin prohibit-password\nAllowUsers root\nStrictModes no\n"
        "AllowTcpForwarding remote\nAllowStreamLocalForwarding no\nGatewayPorts no\n"
        "PermitTTY no\nX11Forwarding no\nAllowAgentForwarding no\nMaxSessions 0\n"
        "UseDNS no\nLogLevel VERBOSE\n")
    known = tmp_path / "known_hosts"
    known.write_text(f"[127.0.0.1]:{server_port} {mac_key}\n")
    base = ["ssh", "-F", "/dev/null", "-T", "-p", str(server_port), "-i", str(tunnel_key),
            "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3",
            "-o", "StrictHostKeyChecking=yes", "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "UserKnownHostsFile=" + str(known), "-o", "ExitOnForwardFailure=yes"]
    server = subprocess.Popen(["/usr/sbin/sshd", "-D", "-f", str(config),
                               "-E", str(tmp_path / "sshd.log")])
    echo = socketserver.TCPServer(("127.0.0.1", 0), Echo)
    thread = threading.Thread(target=echo.serve_forever, daemon=True)
    thread.start()
    tunnel = None
    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                with socket.create_connection(("127.0.0.1", server_port), timeout=0.2):
                    break
            except OSError:
                if server.poll() is not None or time.monotonic() > deadline:
                    pytest.fail("Isolated sshd could not start: " +
                                (tmp_path / "sshd.log").read_text())
                time.sleep(0.05)
        target = f"127.0.0.1:{echo.server_address[1]}"
        tunnel = subprocess.Popen(base + ["-N", "-R", f"127.0.0.1:{listen_port}:{target}",
                                          "root@127.0.0.1"], stderr=subprocess.PIPE)
        deadline = time.monotonic() + 5
        while True:
            try:
                with socket.create_connection(("127.0.0.1", listen_port), timeout=1) as conn:
                    assert conn.recv(100) == b"mac-care-fixture-only"
                    break
            except (OSError, AssertionError):
                if tunnel.poll() is not None:
                    pytest.fail(tunnel.stderr.read().decode() + (tmp_path / "sshd.log").read_text())
                if time.monotonic() > deadline:
                    pytest.fail("Reverse forwarding never reached the echo fixture")
                time.sleep(0.05)
        extra = subprocess.run(base + ["-N", "-R", f"127.0.0.1:{forbidden_port}:{target}",
                                       "root@127.0.0.1"], capture_output=True, timeout=5)
        assert extra.returncode != 0
        local = subprocess.run(base + ["-W", target, "root@127.0.0.1"],
                               capture_output=True, timeout=5)
        assert local.returncode != 0
        shell = subprocess.run(base + ["root@127.0.0.1", "true"],
                               capture_output=True, timeout=5)
        assert shell.returncode != 0
    finally:
        if tunnel is not None:
            tunnel.terminate()
            tunnel.wait(timeout=5)
        server.terminate()
        server.wait(timeout=5)
        echo.shutdown()
        echo.server_close()
        thread.join(timeout=2)
