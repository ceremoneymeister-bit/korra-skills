#!/bin/bash
# Generated per session. Contains public keys only. Review before running.
set -eu
umask 077
export LC_ALL=C
[ "$(/usr/bin/uname -s)" = Darwin ] || { echo "Этот файл предназначен для Mac."; exit 1; }
[ "$(/usr/bin/id -u)" != 0 ] || { echo "Запусти обычным пользователем Mac."; exit 1; }
sid=@@SID@@
case "$sid" in *'@@'*) echo "Это шаблон. Сначала агент должен выполнить prepare."; exit 1;; esac
agent_key=@@AGENT_KEY@@
expires=@@EXPIRES@@
relay_host=@@RELAY_HOST@@
relay_user=@@RELAY_USER@@
relay_port=@@RELAY_PORT@@
listen_port=@@LISTEN_PORT@@
relay_known=@@RELAY_KNOWN@@
state="$HOME/.local/state/mac-care/$sid"
auth="$HOME/.ssh/authorized_keys"
for folder in "$HOME/.ssh" "$HOME/.local" "$HOME/.local/state" "$HOME/.local/state/mac-care" "$state"; do
    [ ! -L "$folder" ] || { echo "Символическая ссылка в пути сеанса; нужна проверка агентом."; exit 1; }
    [ ! -e "$folder" ] || [ -d "$folder" ] || { echo "Путь занят файлом."; exit 1; }
done
[ ! -L "$auth" ] || { echo "authorized_keys является ссылкой; нужна проверка агентом."; exit 1; }
[ ! -e "$auth" ] || [ -f "$auth" ] || { echo "authorized_keys не обычный файл."; exit 1; }
[ ! -e "$state/revoked" ] || { echo "Этот сеанс завершён. Попроси новый файл."; exit 1; }
host_key=$(/usr/bin/ssh-keyscan -T 5 -t ed25519 127.0.0.1 2>/dev/null |
    /usr/bin/awk '$2 == "ssh-ed25519" {print $2 " " $3; exit}')
[ -n "$host_key" ] || {
    echo "Сначала: Настройки системы → Основные → Общий доступ → Удалённый вход."
    echo "Разреши вход только своему пользователю, затем повтори эту команду."
    echo "Полный доступ к диску для базового аудита не требуется."
    exit 1
}
/bin/mkdir -p "$HOME/.ssh" "$state"
/bin/chmod 700 "$HOME/.ssh" "$state"
for file in "$state/disconnect.sh" "$state/connect.sh" "$state/relay_known_hosts" "$state/tunnel_key" "$state/tunnel_key.pub"; do
    [ ! -L "$file" ] || { echo "Ссылка вместо файла сеанса."; exit 1; }
done
entry="restrict,expiry-time=\"$expires\" $agent_key mac-care-$sid"
key_lock="$HOME/.ssh/.mac-care-key-lock"
/bin/mkdir "$key_lock" 2>/dev/null || { echo "Другой сеанс меняет SSH-ключи. Повтори позже."; exit 1; }
trap '/bin/rmdir "$key_lock" 2>/dev/null || true' EXIT
/usr/bin/touch "$auth"
/bin/chmod 600 "$auth"
if ! /usr/bin/grep -qxF "$entry" "$auth"; then
    /usr/bin/printf '\n%s\n' "$entry" >> "$auth"
fi
# Write a local recovery command before requesting a network connection.
{
    /usr/bin/printf '#!/bin/bash\nset -eu\numask 077\nsid=%q\nentry=%q\n' "$sid" "$entry"
    /bin/cat <<'DISCONNECT'
state="$HOME/.local/state/mac-care/$sid"
auth="$HOME/.ssh/authorized_keys"
[ ! -L "$auth" ] && [ ! -L "$state" ] || exit 1
key_lock="$HOME/.ssh/.mac-care-key-lock"
/bin/mkdir "$key_lock" 2>/dev/null || { echo "Повтори после завершения другого сеанса."; exit 1; }
temp=$(/usr/bin/mktemp "$HOME/.ssh/.mac-care.XXXXXX")
trap '/bin/rm -f "$temp"; /bin/rmdir "$key_lock" 2>/dev/null || true' EXIT
/usr/bin/awk -v target="$entry" '$0 != target' "$auth" > "$temp"
/bin/cat "$temp" > "$auth"
/usr/bin/touch "$state/revoked"
/bin/rm -f "$state/tunnel_key" "$state/tunnel_key.pub"
echo "Ключ агента отозван. Закрой окно туннеля, если оно открыто."
echo "Настройку Удалённый вход верни в прежнее состояние, если включал её для этого сеанса."
DISCONNECT
} > "$state/disconnect.sh"
if [ -n "$relay_host" ]; then
    if [ ! -f "$state/tunnel_key" ]; then
        /usr/bin/ssh-keygen -q -t ed25519 -N '' -C "mac-care-$sid" -f "$state/tunnel_key"
    fi
    /usr/bin/printf '%s\n' "$relay_known" > "$state/relay_known_hosts"
    {
        /usr/bin/printf '#!/bin/bash\nset -eu\n'
        /usr/bin/printf 'state=%q\nrelay_host=%q\nrelay_user=%q\nrelay_port=%q\nlisten_port=%q\n' \
            "$state" "$relay_host" "$relay_user" "$relay_port" "$listen_port"
        /bin/cat <<'CONNECT'
[ ! -e "$state/revoked" ] || { echo "Сеанс завершён."; exit 1; }
echo "Оставь это окно открытым. Отсутствие вывода после подключения нормально."
echo "Для завершения нажми Control-C."
exec /usr/bin/ssh -F /dev/null -NT -p "$relay_port" -i "$state/tunnel_key" \
    -o IdentitiesOnly=yes -o BatchMode=yes -o ExitOnForwardFailure=yes \
    -o StrictHostKeyChecking=yes -o GlobalKnownHostsFile=/dev/null \
    -o "UserKnownHostsFile=$state/relay_known_hosts" \
    -o ServerAliveInterval=15 -o ServerAliveCountMax=2 \
    -R "127.0.0.1:$listen_port:127.0.0.1:22" "$relay_user@$relay_host"
CONNECT
    } > "$state/connect.sh"
fi
echo "Передай агенту строки между MAC_CARE_REPORT и END_REPORT (здесь только публичные ключи):"
echo "MAC_CARE_REPORT"
/usr/bin/printf 'SESSION=%s\nUSER=%s\nHOST_KEY=%s\n' "$sid" "$(/usr/bin/id -un)" "$host_key"
/usr/bin/printf 'HOST_FINGERPRINT=%s\n' "$(printf '%s\n' "$host_key" |
    /usr/bin/ssh-keygen -lf - | /usr/bin/awk '{print $2}')"
if [ -n "$relay_host" ]; then
    /usr/bin/printf 'TUNNEL_KEY=%s\n' "$(/usr/bin/awk '{print $1 " " $2}' "$state/tunnel_key.pub")"
fi
echo "END_REPORT"
if [ -n "$relay_host" ]; then
    echo "После ответа агента о готовности сервера выполни:"
    /usr/bin/printf '/bin/bash %q\n' "$state/connect.sh"
fi
echo "Чтобы самостоятельно отозвать доступ:"
/usr/bin/printf '/bin/bash %q\n' "$state/disconnect.sh"
