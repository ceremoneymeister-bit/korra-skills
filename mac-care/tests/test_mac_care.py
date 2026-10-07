import argparse
import base64
import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import sys

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "mac_care.py"
spec = importlib.util.spec_from_file_location("mac_care", SOURCE)
mc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mc)


def key(number=0):
    return "ssh-ed25519 " + base64.b64encode(
        b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes([number]) * 32).decode()


@pytest.fixture
def session(tmp_path):
    directory = tmp_path / "private session"
    mc.prepare(argparse.Namespace(session=directory, relay_host=None, hours=4))
    state = mc.read_json(directory / "session.json")
    report = tmp_path / "report.txt"
    report.write_text(f'SESSION={state["id"]}\nUSER=macuser\nHOST_KEY={key()}\n'
                      f'HOST_FINGERPRINT={mc.fingerprint(key())}\n')
    mc.enroll(argparse.Namespace(report=report, host="127.0.0.1", port=22222),
              directory, state)
    return directory, state


def new_plan(session, tmp_path, *, admin=False):
    directory, state = session
    paths = {}
    for name, text in dict(script="printf changed\n", verify="test 1 = 1\n",
                           rollback="printf restored\n").items():
        path = tmp_path / (name + ".sh")
        path.write_text(text)
        paths[name] = path
    mc.create_plan(argparse.Namespace(**paths, summary="One setting",
                                      impact="Reversible", admin_dialog=admin), directory, state)
    path = sorted(directory.glob("plan-*.json"))[-1]
    plan = mc.read_json(path)
    return path, plan


def approved(session, tmp_path):
    path, plan = new_plan(session, tmp_path)
    mc.approve(argparse.Namespace(plan=plan["id"], reply="Да, этот пункт", source="message-123"),
               *session)
    return path, plan


def operation(plan, name="apply"):
    return argparse.Namespace(plan=plan["id"], command=name, timeout=20)


def success(*args, **kwargs):
    return subprocess.CompletedProcess([], 0, "ok", "")


@pytest.mark.parametrize("value", ["-oProxyCommand=bad", "a;touch /tmp/bad", "host\nother",
                                  "$(whoami)", "user@host", "../host", "a b"])
def test_reject_shell_and_option_injection(value):
    with pytest.raises(ValueError):
        mc.host(value)


@pytest.mark.parametrize("value", ["host.example", "127.0.0.1", "::1", "2001:db8::1"])
def test_valid_hosts(value):
    assert mc.host(value) == value


@pytest.mark.parametrize("value", ["ssh-ed25519 !!!!", "ssh-ed25519 YQ==",
                                  "ssh-rsa AAAA", key() + " unexpected-comment"])
def test_malformed_keys_rejected(value):
    with pytest.raises((ValueError, TypeError)):
        mc.public_key(value)


def test_generated_key_is_valid_and_private(session):
    directory, state = session
    assert mc.public_key(state["agent_key"])
    assert directory.stat().st_mode & 0o077 == 0
    assert (directory / "agent_key").stat().st_mode & 0o077 == 0
    bootstrap = (directory / "connect-mac.sh").read_text()
    assert not re.search(r"@@[A-Z_]+@@", bootstrap)
    assert "PRIVATE KEY" not in bootstrap
    subprocess.run(["bash", "-n", str(directory / "connect-mac.sh")], check=True)
    result = subprocess.run(["ssh-keygen", "-lf", str(directory / "agent_key.pub")],
                            capture_output=True, text=True, check=True)
    assert mc.fingerprint(state["agent_key"]) in result.stdout


def test_bootstrap_on_linux_leaves_home_untouched(session, tmp_path):
    if sys.platform != "linux":
        pytest.skip("Linux negative platform check")
    home = tmp_path / "not a Mac"
    home.mkdir()
    result = subprocess.run(["bash", str(session[0] / "connect-mac.sh")],
                            env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert list(home.iterdir()) == []


def test_ssh_connection_pins_host_and_avoids_config(session):
    directory, state = session
    args = mc.ssh_command(directory, state)
    assert args[:3] == ["ssh", "-F", "/dev/null"]
    assert "StrictHostKeyChecking=yes" in args
    assert "BatchMode=yes" in args
    assert "GlobalKnownHostsFile=/dev/null" in args
    assert args[-3:] == ["macuser@127.0.0.1", "/bin/bash", "-s"]
    assert args[args.index("-i") + 1] == str(directory / "agent_key")


def test_changed_host_report_is_not_silently_accepted(session, tmp_path):
    directory, state = session
    with pytest.raises(ValueError, match="уже привязан"):
        mc.enroll(argparse.Namespace(report=tmp_path / "missing", host="new.host", port=22),
                  directory, state)


def test_no_approval_no_connection(session, tmp_path, monkeypatch):
    path, plan = new_plan(session, tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail("Must not connect before approval")
    monkeypatch.setattr(mc, "remote", forbidden)
    with pytest.raises(ValueError, match="согласия"):
        mc.execute(operation(plan), *session)


def test_negative_answer_is_not_approval(session, tmp_path):
    path, plan = new_plan(session, tmp_path)
    with pytest.raises(ValueError, match="не разрешает"):
        mc.approve(argparse.Namespace(plan=plan["id"], reply="Нет!", source="message-123"),
                   *session)
    assert mc.read_json(path)["status"] == "draft"


@pytest.mark.parametrize("field,value", [
    ("script", "different action"), ("rollback", "different rollback"),
    ("verify", "true"), ("impact", "hidden consequence"), ("admin", True)])
def test_editing_reviewed_content_invalidates_approval(session, tmp_path, field, value):
    path, plan = approved(session, tmp_path)
    changed = mc.read_json(path)
    changed[field] = value
    mc.save(path, changed)
    with pytest.raises(ValueError, match="изменились"):
        mc.execute(operation(plan), *session)


def test_new_target_invalidates_plan(session, tmp_path):
    path, plan = approved(session, tmp_path)
    session[1]["target"] = dict(host="other.host", port=22, user="macuser", fingerprint="different")
    with pytest.raises(ValueError, match="изменились"):
        mc.get_plan(*session, plan["id"])


def test_apply_verify_rollback_lifecycle(session, tmp_path, monkeypatch):
    path, plan = approved(session, tmp_path)
    monkeypatch.setattr(mc, "remote", success)
    assert mc.execute(operation(plan), *session) == 0
    assert mc.read_json(path)["status"] == "applied"
    with pytest.raises(ValueError, match="Повтор"):
        mc.execute(operation(plan), *session)
    assert mc.execute(operation(plan, "verify"), *session) == 0
    assert mc.read_json(path)["status"] == "verified"
    assert mc.execute(operation(plan, "rollback"), *session) == 0
    assert mc.read_json(path)["status"] == "rolled_back"
    with pytest.raises(ValueError):
        mc.execute(operation(plan, "rollback"), *session)


def test_timeout_after_start_is_unknown_and_not_replayed(session, tmp_path, monkeypatch):
    path, plan = approved(session, tmp_path)
    def transport(directory, state, script, timeout=120):
        if script == "true\n":
            return success()
        assert mc.read_json(path)["status"] == "applying"
        raise subprocess.TimeoutExpired(["ssh"], timeout)
    monkeypatch.setattr(mc, "remote", transport)
    with pytest.raises(ValueError, match="исход неизвестен"):
        mc.execute(operation(plan), *session)
    assert mc.read_json(path)["status"] == "unknown"
    with pytest.raises(ValueError, match="Повтор"):
        mc.execute(operation(plan), *session)


def test_unreachable_probe_does_not_consume_approval(session, tmp_path, monkeypatch):
    path, plan = approved(session, tmp_path)
    monkeypatch.setattr(mc, "remote", lambda *a, **k: subprocess.CompletedProcess([], 255, "", "offline"))
    with pytest.raises(ValueError, match="не запускали"):
        mc.execute(operation(plan), *session)
    assert mc.read_json(path)["status"] == "approved"


def test_unknown_rollback_cannot_repeat(session, tmp_path, monkeypatch):
    path, plan = approved(session, tmp_path)
    monkeypatch.setattr(mc, "remote", success)
    mc.execute(operation(plan), *session)
    def transport(directory, state, script, timeout=120):
        if script == "true\n":
            return success()
        raise subprocess.TimeoutExpired(["ssh"], timeout)
    monkeypatch.setattr(mc, "remote", transport)
    with pytest.raises(ValueError, match="исход неизвестен"):
        mc.execute(operation(plan, "rollback"), *session)
    with pytest.raises(ValueError, match="Откат уже"):
        mc.execute(operation(plan, "rollback"), *session)


@pytest.mark.parametrize("previously_verified", [False, True])
@pytest.mark.parametrize("exit_code,expected", [(1, "failed"), (255, "unknown")])
def test_failed_verification_does_not_claim_success(
        session, tmp_path, monkeypatch, previously_verified, exit_code, expected):
    path, plan = approved(session, tmp_path)
    monkeypatch.setattr(mc, "remote", success)
    mc.execute(operation(plan), *session)
    if previously_verified:
        mc.execute(operation(plan, "verify"), *session)
    def transport(directory, state, script, timeout=120):
        return success() if script == "true\n" else subprocess.CompletedProcess([], exit_code, "", "check failed")
    monkeypatch.setattr(mc, "remote", transport)
    assert mc.execute(operation(plan, "verify"), *session) == exit_code
    assert mc.read_json(path)["status"] == expected


def test_remote_guard_prevents_mutation_on_linux(session, tmp_path):
    if sys.platform != "linux":
        pytest.skip("Linux guard test")
    target = tmp_path / "must not exist"
    import shlex
    result = subprocess.run(["bash"], input=mc.GUARD + "touch " + shlex.quote(str(target)),
                            text=True, capture_output=True)
    assert result.returncode == 70
    assert not target.exists()


def test_relay_registration_preserves_other_keys_and_is_idempotent(session, tmp_path, monkeypatch):
    directory, state = session
    account = pwd.getpwnam("nobody") if os.geteuid() == 0 else pwd.getpwuid(os.geteuid())
    state["relay"] = dict(user=account.pw_name, listen_port=22222)
    entry = f'restrict,port-forwarding,permitlisten="127.0.0.1:22222" {key(1)} mac-care-{state["id"]}\n'
    mc.private_write(directory / "relay-entry.txt", entry)
    folder = tmp_path / ".ssh"
    folder.mkdir(mode=0o700)
    auth = folder / "authorized_keys"
    original = "# existing key\n" + key(2) + " existing-owner\n"
    mc.private_write(auth, original)
    if os.geteuid() == 0:
        os.chown(folder, account.pw_uid, account.pw_gid)
        os.chown(auth, account.pw_uid, account.pw_gid)
    args = argparse.Namespace(authorized_keys=auth, remove=False, client_address="192.0.2.2",
                              client_host=None, sshd_config=Path("/etc/ssh/sshd_config"))
    monkeypatch.setattr(mc.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        [], 0, "allowtcpforwarding remote\nallowstreamlocalforwarding no\ngatewayports no\n", ""))
    mc.relay_key(args, directory, state)
    mc.relay_key(args, directory, state)
    assert auth.read_text() == original + entry
    args.remove = True
    mc.relay_key(args, directory, state)
    assert auth.read_text() == original


def test_relay_rejects_symlink(session, tmp_path):
    directory, state = session
    account = pwd.getpwnam("nobody") if os.geteuid() == 0 else pwd.getpwuid(os.geteuid())
    state["relay"] = {"user": account.pw_name}
    mc.private_write(directory / "relay-entry.txt", key(1) + "\n")
    target = tmp_path / "keep"
    target.write_text("unchanged")
    link = tmp_path / "authorized_keys"
    link.symlink_to(target)
    with pytest.raises(OSError):
        mc.relay_key(argparse.Namespace(authorized_keys=link, remove=True), directory, state)
    assert target.read_text() == "unchanged"


def test_bootstrap_reverse_binds_loopback_and_pins_relay(tmp_path):
    directory = tmp_path / "session"
    pub = tmp_path / "relay.pub"
    pub.write_text(key())
    mc.prepare(argparse.Namespace(session=directory, relay_host="relay.example.org",
                                  relay_user="support", relay_port=2222,
                                  relay_key=pub, listen_port=23000, hours=1))
    script = (directory / "connect-mac.sh").read_text()
    subprocess.run(["bash", "-n", str(directory / "connect-mac.sh")], check=True)
    assert "relay_port=2222" in script
    assert "[relay.example.org]:2222" in script
    assert '-R "127.0.0.1:$listen_port:127.0.0.1:22"' in script
    assert "StrictHostKeyChecking=yes" in script
    assert "PRIVATE KEY" not in script


def test_admin_dialog_escapes_script_as_data():
    script = 'printf "%s\\n" "$HOME"\nprintf \'quoted\'\n'
    wrapped = mc.admin_script(script)
    assert "with administrator privileges" in wrapped
    assert "linefeed" in wrapped
    assert "<<'MAC_CARE_" in wrapped
    subprocess.run(["bash", "-n"], input=wrapped, text=True, check=True)


def test_relay_rejects_root_account(session, tmp_path):
    directory, state = session
    state["relay"] = {"user": "root"}
    with pytest.raises(ValueError, match="root"):
        mc.relay_key(argparse.Namespace(authorized_keys=tmp_path / "missing", remove=False),
                     directory, state)


def test_relay_does_not_add_key_when_server_allows_local_forwarding(session, tmp_path, monkeypatch):
    directory, state = session
    account = pwd.getpwnam("nobody") if os.geteuid() == 0 else pwd.getpwuid(os.geteuid())
    state["relay"] = {"user": account.pw_name}
    args = argparse.Namespace(authorized_keys=tmp_path / "must-not-be-created", remove=False,
                              client_address="192.0.2.2", client_host=None,
                              sshd_config=Path("/etc/ssh/sshd_config"))
    monkeypatch.setattr(mc.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        [], 0, "allowtcpforwarding yes\nallowstreamlocalforwarding yes\ngatewayports no\n", ""))
    with pytest.raises(ValueError, match="не ограничен"):
        mc.relay_key(args, directory, state)
    assert not args.authorized_keys.exists()


def test_real_shell_change_verification_and_rollback_with_unicode_path(session, tmp_path, monkeypatch):
    import shlex
    directory, state = session
    target = tmp_path / "тест — файл 'один'"
    target.write_text("before")
    q = shlex.quote(str(target))
    bodies = dict(
        script=f'set -e\n[ "$(cat {q})" = before ]\nprintf after > {q}\n',
        verify=f'[ "$(cat {q})" = after ]\n',
        rollback=f'set -e\n[ "$(cat {q})" = after ]\nprintf before > {q}\n')
    paths = {}
    for name, content in bodies.items():
        paths[name] = tmp_path / (name + ".sh")
        paths[name].write_text(content)
    mc.create_plan(argparse.Namespace(**paths, summary="Test fixture",
                                      impact="Only isolated test file", admin_dialog=False),
                   directory, state)
    path = next(directory.glob("plan-*.json"))
    plan = mc.read_json(path)
    mc.approve(argparse.Namespace(plan=plan["id"], reply="Да", source="synthetic-test"), directory, state)
    # Execution uses the reviewed snapshot, not a subsequently edited source file.
    paths["script"].write_text("exit 89\n")
    def local_transport(directory, state, script, timeout=120):
        return subprocess.run(["bash"], input=script, text=True, capture_output=True, timeout=timeout)
    monkeypatch.setattr(mc, "remote", local_transport)
    assert mc.execute(operation(plan), directory, state) == 0
    assert target.read_text() == "after"
    assert mc.execute(operation(plan, "verify"), directory, state) == 0
    assert mc.execute(operation(plan, "rollback"), directory, state) == 0
    assert target.read_text() == "before"


@pytest.mark.parametrize("defect", ["fingerprint", "duplicate", "session"])
def test_untrusted_connection_report_rejected_before_enrollment(tmp_path, defect):
    directory = tmp_path / "session"
    mc.prepare(argparse.Namespace(session=directory, relay_host=None, hours=4))
    state = mc.read_json(directory / "session.json")
    report = tmp_path / "report"
    sid = "different" if defect == "session" else state["id"]
    fp = "SHA256:wrong" if defect == "fingerprint" else mc.fingerprint(key())
    text = f"SESSION={sid}\nUSER=macuser\nHOST_KEY={key()}\nHOST_FINGERPRINT={fp}\n"
    if defect == "duplicate":
        text += "USER=attacker\n"
    report.write_text(text)
    with pytest.raises(ValueError):
        mc.enroll(argparse.Namespace(report=report, host="127.0.0.1", port=22), directory, state)
    assert "target" not in mc.read_json(directory / "session.json")
