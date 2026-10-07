#!/usr/bin/env python3
"""Remote Mac support: pinned SSH, private reports and reviewed change plans.

Requires Python 3.10+ and OpenSSH on the AGENT host; no Python on the Mac.
An approval receipt records a real user's decision. It cannot authenticate that
decision or sandbox the agent; the host's authorization controls remain required.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import datetime as dt
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import stat
import subprocess
import sys
import uuid

PACKAGE = Path(__file__).resolve().parent.parent


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def fail(message):
    raise ValueError(message)


def atom(value, pattern, label):
    if not re.fullmatch(pattern, value):
        fail("Недопустимое значение: " + label)
    return value


def host(value):
    if ":" in value:
        ipaddress.IPv6Address(value)
        return value
    return atom(value, r"[A-Za-z0-9][A-Za-z0-9.-]*", "host")


def user(value):
    return atom(value, r"[A-Za-z_][A-Za-z0-9_.-]*", "user")


def port(value):
    result = int(value)
    if not 1 <= result <= 65535:
        fail("Порт должен быть от 1 до 65535")
    return result


def public_key(value):
    fields = value.strip().split()
    if len(fields) != 2 or fields[0] != "ssh-ed25519":
        fail("Нужен публичный ключ ssh-ed25519 без комментария")
    raw = base64.b64decode(fields[1], validate=True)
    prefix = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"
    if not raw.startswith(prefix) or len(raw) != len(prefix) + 32:
        fail("Повреждён публичный ключ")
    return " ".join(fields)


def fingerprint(key):
    raw = base64.b64decode(public_key(key).split()[1], validate=True)
    return "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")


def private_write(path, text):
    path = Path(path)
    if path.is_symlink():
        fail("Отказ записи через символическую ссылку")
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(text)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temp, path)


def save(path, value):
    private_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


@contextlib.contextmanager
def lock(directory):
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        fail("Нет приватного каталога сеанса")
    if directory.stat().st_uid != os.geteuid() or directory.stat().st_mode & 0o077:
        fail("Каталог сеанса должен принадлежать агенту и иметь права 700")
    fd = os.open(directory / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as file:
        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def prepare(args):
    directory = args.session.absolute()
    if directory.exists():
        fail("Каталог уже существует; используй текущий сеанс или новый путь")
    relay = None
    if args.relay_host:
        if not args.relay_user or not args.relay_key:
            fail("Для туннеля нужны relay-user и проверенный relay-key")
        if args.relay_user == "root":
            fail("Для нового туннеля нужен отдельный пользователь сервера, не root")
        if args.listen_port < 1024:
            fail("Для туннеля выбери свободный порт 1024–65535")
        relay = dict(host=host(args.relay_host), user=user(args.relay_user),
                     port=port(args.relay_port), listen_port=port(args.listen_port),
                     key=public_key(args.relay_key.read_text().strip()))
    directory.mkdir(mode=0o700, parents=True)
    sid = uuid.uuid4().hex
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "",
                    "-C", "mac-care-" + sid, "-f", str(directory / "agent_key")],
                   check=True, stdin=subprocess.DEVNULL, timeout=30)
    key = public_key(" ".join((directory / "agent_key.pub").read_text().split()[:2]))
    expires = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=args.hours)
    state = dict(version=1, id=sid, created=now(), relay=relay,
                 expires=expires.strftime("%Y%m%d%H%M%SZ"), agent_key=key)
    save(directory / "session.json", state)
    template = (PACKAGE / "scripts" / "bootstrap-template.sh").read_text()
    values = {
        "SID": sid, "AGENT_KEY": key, "EXPIRES": state["expires"],
        "RELAY_HOST": relay["host"] if relay else "",
        "RELAY_USER": relay["user"] if relay else "",
        "RELAY_PORT": str(relay["port"]) if relay else "22",
        "LISTEN_PORT": str(relay["listen_port"]) if relay else "22222",
        "RELAY_KNOWN": (f'[{relay["host"]}]:{relay["port"]} {relay["key"]}'
                        if relay and relay["port"] != 22 else
                        f'{relay["host"]} {relay["key"]}' if relay else ""),
    }
    for name, value in values.items():
        template = template.replace("@@" + name + "@@", shlex.quote(value))
    private_write(directory / "connect-mac.sh", template)
    print("Подготовлен файл:", directory / "connect-mac.sh")
    print("Передай пользователю этот публичный скрипт; agent_key остаётся у агента.")
    if relay:
        print("Fingerprint сервера:", fingerprint(relay["key"]))


def enroll(args, directory, state):
    if state.get("target"):
        fail("Mac уже привязан; смена цели требует нового сеанса")
    report = {}
    for line in args.report.read_text().splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            if key in report:
                fail("Повтор поля в отчёте подключения")
            report[key] = value
    if report.get("SESSION") != state["id"]:
        fail("Отчёт от другого сеанса")
    key = public_key(report.get("HOST_KEY", ""))
    if fingerprint(key) != report.get("HOST_FINGERPRINT"):
        fail("Fingerprint не соответствует ключу")
    target_host = host(args.host)
    target_port = port(args.port)
    target_user = user(report.get("USER", ""))
    if target_user == "root":
        fail("Подключайся обычным пользователем Mac")
    name = target_host if target_port == 22 else f"[{target_host}]:{target_port}"
    private_write(directory / "known_hosts", name + " " + key + "\n")
    state["target"] = dict(host=target_host, port=target_port, user=target_user,
                           fingerprint=fingerprint(key))
    if state["relay"]:
        tunnel_key = public_key(report.get("TUNNEL_KEY", ""))
        listen = state["relay"]["listen_port"]
        entry = (f'restrict,port-forwarding,'
                 f'permitlisten="127.0.0.1:{listen}",command="/usr/bin/false",'
                 f'expiry-time="{state["expires"]}" {tunnel_key} mac-care-{state["id"]}\n')
        private_write(directory / "relay-entry.txt", entry)
    save(directory / "session.json", state)
    print("Привязан Mac:", target_user, target_host, target_port, fingerprint(key))
    if state["relay"]:
        print("Зарегистрируй relay-entry.txt командой relay-key на сервере.")


def relay_key(args, directory, state):
    if not state["relay"]:
        fail("У сеанса нет обратного туннеля")
    try:
        relay_account = pwd.getpwnam(state["relay"]["user"])
    except KeyError:
        fail("Нет выделенного пользователя на этом сервере; проверь среду ретранслятора")
    if relay_account.pw_uid == 0:
        fail("Учётная запись root не допускается для нового туннеля")
    if not args.remove:
        if not args.client_address:
            fail("Для проверки правил ретранслятора нужен client-address Mac")
        address = str(ipaddress.ip_address(args.client_address))
        client_host = host(args.client_host or address)
        effective = subprocess.run(
            ["/usr/sbin/sshd", "-T", "-f", str(args.sshd_config),
             "-C", f'user={user(state["relay"]["user"])},addr={address},host={client_host}'],
            capture_output=True, text=True, timeout=15, check=True)
        settings = dict(line.split(None, 1) for line in effective.stdout.splitlines()
                        if len(line.split(None, 1)) == 2)
        required = {"allowtcpforwarding": "remote", "allowstreamlocalforwarding": "no",
                    "gatewayports": "no"}
        if any(settings.get(name) != value for name, value in required.items()):
            fail("Сервер не ограничен: нужны AllowTcpForwarding remote, "
                 "AllowStreamLocalForwarding no, GatewayPorts no для выделенного пользователя")
    entry = (directory / "relay-entry.txt").read_bytes()
    path = args.authorized_keys.absolute()
    if path.parent.is_symlink() or not path.parent.is_dir():
        fail("Нет обычного каталога .ssh пользователя ретранслятора")
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    with os.fdopen(fd, "r+b") as out:
        fcntl.flock(out, fcntl.LOCK_EX | fcntl.LOCK_NB)
        st = os.fstat(out.fileno())
        if not stat.S_ISREG(st.st_mode):
            fail("Ожидался обычный файл ключей")
        if (st.st_uid != relay_account.pw_uid or
                st.st_uid != path.parent.stat().st_uid or st.st_mode & 0o022):
            fail("Проверь владельца и права authorized_keys")
        data = out.read()
        lines = data.splitlines(keepends=True)
        if args.remove:
            result = b"".join(line for line in lines if line.rstrip(b"\r\n") != entry.rstrip(b"\n"))
        elif any(line.rstrip(b"\r\n") == entry.rstrip(b"\n") for line in lines):
            result = data
        else:
            result = data + (b"\n" if data and not data.endswith(b"\n") else b"") + entry
        if result != data:
            out.seek(0)
            out.write(result)
            out.truncate()
            out.flush()
            os.fsync(out.fileno())
    print("Ключ туннеля удалён." if args.remove else "Ключ туннеля зарегистрирован.")


def ssh_command(directory, state):
    target = state.get("target")
    if not target:
        fail("Сначала enroll с отчётом владельца Mac")
    return ["ssh", "-F", "/dev/null", "-T", "-p", str(port(target["port"])),
            "-i", str(directory / "agent_key"), "-o", "IdentitiesOnly=yes",
            "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2",
            "-o", "StrictHostKeyChecking=yes", "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "UserKnownHostsFile=" + str(directory / "known_hosts"),
            "-o", "ClearAllForwardings=yes",
            user(target["user"]) + "@" + host(target["host"]), "/bin/bash", "-s"]


GUARD = '[ "$(/usr/bin/uname -s)" = Darwin ] || { echo "Ожидался Mac" >&2; exit 70; }\n'


def remote(directory, state, script, timeout=120):
    return subprocess.run(ssh_command(directory, state), input=GUARD + script,
                          text=True, capture_output=True, timeout=timeout)


def audit(directory, state):
    script = (PACKAGE / "scripts" / "audit.sh").read_text()
    result = remote(directory, state, script)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    path = directory / ("audit-" + stamp + ".txt")
    private_write(path, result.stdout + "\n" + result.stderr)
    print("Отчёт:", path, "exit:", result.returncode)
    return result.returncode


def plan_hash(plan):
    fields = {k: plan[k] for k in ("target", "summary", "impact", "script", "rollback", "verify", "admin")}
    return hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def create_plan(args, directory, state):
    if not state.get("target"):
        fail("Сначала привяжи конкретный Mac")
    plan = dict(id=uuid.uuid4().hex, target=state["target"], summary=args.summary,
                impact=args.impact, admin=args.admin_dialog, created=now(), status="draft")
    for field in ("script", "rollback", "verify"):
        content = getattr(args, field).read_text()
        if not content.strip() or "\0" in content:
            fail("Нужны непустые script, rollback и verify")
        plan[field] = content
    plan["hash"] = plan_hash(plan)
    save(directory / ("plan-" + plan["id"] + ".json"), plan)
    review = (f'План {plan["id"]}\nMac: {plan["target"]["user"]}@{plan["target"]["host"]}\n'
              f'Изменение: {plan["summary"]}\nПоследствия: {plan["impact"]}\n'
              f'Диалог администратора: {plan["admin"]}\nSHA256: {plan["hash"]}\n\n'
              f'ПРИМЕНЕНИЕ\n{plan["script"]}\nПРОВЕРКА\n{plan["verify"]}\nОТКАТ\n{plan["rollback"]}')
    private_write(directory / ("review-" + plan["id"] + ".txt"), review)
    print(review)


def get_plan(directory, state, plan_id):
    atom(plan_id, r"[a-f0-9]{32}", "plan")
    path = directory / ("plan-" + plan_id + ".json")
    plan = read_json(path)
    if plan.get("hash") != plan_hash(plan) or plan["target"] != state.get("target"):
        fail("План или целевой Mac изменились; требуется новый план и согласие")
    return path, plan


def approve(args, directory, state):
    path, plan = get_plan(directory, state, args.plan)
    if plan["status"] != "draft":
        fail("Согласие принимается только для нового плана")
    if not args.reply.strip() or not args.source.strip():
        fail("Укажи реальный ответ пользователя и источник сообщения")
    if args.reply.strip().lower().rstrip(".!") in {"нет", "no", "не надо", "отмена", "cancel"}:
        fail("Ответ пользователя не разрешает изменение")
    plan["approval"] = dict(reply=args.reply, source=args.source, at=now(), hash=plan["hash"])
    plan["status"] = "approved"
    save(path, plan)
    print("Согласие записано. Оно относится только к этому плану.")


def admin_script(script):
    shell_command = "/bin/bash -c " + shlex.quote(script)
    def quote(line):
        return '"' + line.replace("\\", "\\\\").replace('"', '\\"') + '"'
    expression = " & linefeed & ".join(quote(line) for line in shell_command.split("\n"))
    end = "MAC_CARE_" + uuid.uuid4().hex
    return (f"/usr/bin/osascript <<'{end}'\n"
            f"do shell script ({expression}) with administrator privileges\n{end}\n")


def execute(args, directory, state):
    path, plan = get_plan(directory, state, args.plan)
    action = args.command
    if plan.get("approval", {}).get("hash") != plan["hash"]:
        fail("Нет записанного согласия на точный план")
    allowed = {"apply": {"approved"},
               "verify": {"applied", "verified", "unknown", "failed", "applying"},
               "rollback": {"applied", "verified", "failed", "unknown", "applying"}}
    if plan["status"] not in allowed[action]:
        fail("Повтор этой операции запрещён для состояния " + plan["status"])
    previous_mutations = [run["action"] for run in plan.get("runs", [])
                          if run["action"] in {"apply", "rollback"}]
    if action == "rollback" and previous_mutations and previous_mutations[-1] == "rollback":
        fail("Откат уже запускался; сначала выясни его фактический исход")
    # Probe before changing the operation status. A timeout here changed nothing.
    probe = remote(directory, state, "true\n", timeout=20)
    if probe.returncode:
        fail("Mac недоступен; изменений не запускали. " + probe.stderr.strip())
    script = plan[{"apply": "script", "rollback": "rollback", "verify": "verify"}[action]]
    if action != "verify" and plan["admin"]:
        script = admin_script(script)
    plan["status"] = {"apply": "applying", "rollback": "rolling_back", "verify": plan["status"]}[action]
    event = dict(action=action, started=now())
    plan.setdefault("runs", []).append(event)
    save(path, plan)
    try:
        result = remote(directory, state, script, timeout=args.timeout)
        event.update(exit=result.returncode, finished=now())
        output = directory / (plan["id"] + "-" + action + "-" + uuid.uuid4().hex[:8] + ".txt")
        private_write(output, result.stdout + "\n" + result.stderr)
        event["output"] = output.name
        plan["status"] = ("unknown" if result.returncode == 255 else "failed"
                          if result.returncode else
                          {"apply": "applied", "verify": "verified", "rollback": "rolled_back"}[action])
        save(path, plan)
        print("Состояние:", plan["status"], "Результат:", output)
        if action == "apply" and result.returncode == 0:
            print("Применение завершено. Теперь выполни verify и пользовательский сценарий.")
        return result.returncode
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        event.update(finished=now(), outcome="unknown")
        plan["status"] = "unknown"
        save(path, plan)
        fail("Связь прервана: исход неизвестен. Сначала проверка фактического состояния, без повтора.")


def disconnect(directory, state):
    sid = atom(state["id"], r"[a-f0-9]{32}", "session id")
    script = 'exec /bin/bash "$HOME/.local/state/mac-care/' + sid + '/disconnect.sh"\n'
    result = remote(directory, state, script, timeout=30)
    if result.returncode:
        fail("Отзыв не подтверждён. Попроси владельца запустить disconnect.sh локально.")
    state["disconnected"] = now()
    save(directory / "session.json", state)
    print("Ключ агента на Mac отозван. Останови окно туннеля и выполни relay-key --remove, если он использовался.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("prepare")
    p.add_argument("--relay-host")
    p.add_argument("--relay-user")
    p.add_argument("--relay-key", type=Path)
    p.add_argument("--relay-port", type=port, default=22)
    p.add_argument("--listen-port", type=port, default=22222)
    p.add_argument("--hours", type=int, choices=range(1, 25), default=4)
    p = commands.add_parser("enroll")
    p.add_argument("--report", type=Path, required=True)
    p.add_argument("--host", type=host, required=True)
    p.add_argument("--port", type=port, default=22)
    p = commands.add_parser("relay-key")
    p.add_argument("--authorized-keys", type=Path, required=True)
    p.add_argument("--remove", action="store_true")
    p.add_argument("--client-address")
    p.add_argument("--client-host")
    p.add_argument("--sshd-config", type=Path, default=Path("/etc/ssh/sshd_config"))
    commands.add_parser("audit")
    commands.add_parser("status")
    commands.add_parser("disconnect")
    p = commands.add_parser("plan")
    for name in ("script", "rollback", "verify"):
        p.add_argument("--" + name, type=Path, required=True)
    p.add_argument("--summary", required=True)
    p.add_argument("--impact", required=True)
    p.add_argument("--admin-dialog", action="store_true")
    p = commands.add_parser("approve")
    p.add_argument("--plan", required=True)
    p.add_argument("--reply", required=True)
    p.add_argument("--source", required=True)
    for name in ("apply", "verify", "rollback"):
        p = commands.add_parser(name)
        p.add_argument("--plan", required=True)
        p.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            prepare(args)
            return 0
        directory = args.session.absolute()
        with lock(directory):
            state = read_json(directory / "session.json")
            if args.command == "enroll":
                enroll(args, directory, state)
            elif args.command == "relay-key":
                relay_key(args, directory, state)
            elif args.command == "audit":
                return audit(directory, state)
            elif args.command == "status":
                print(json.dumps(state, ensure_ascii=False, indent=2))
            elif args.command == "plan":
                create_plan(args, directory, state)
            elif args.command == "approve":
                approve(args, directory, state)
            elif args.command == "disconnect":
                disconnect(directory, state)
            else:
                return execute(args, directory, state)
        return 0
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print("Ошибка:", str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
