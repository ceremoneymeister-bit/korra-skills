#!/usr/bin/env python3
"""Скачать одну историю через существующую Telethon-сессию; просмотр отмечается."""
import argparse
import asyncio
import fcntl
import json
import os
from pathlib import Path
import re
import sys
from datetime import timezone


def target(args):
    if args.link:
        if args.peer is not None or args.id is not None:
            raise ValueError("используйте ссылку ИЛИ --peer + --id")
        match = re.fullmatch(r"https://t\.me/([A-Za-z0-9_]+)/s/([1-9][0-9]*)/?", args.link)
        if not match:
            raise ValueError("нужна ссылка https://t.me/<username>/s/<id>")
        return match[1], int(match[2])
    if not args.peer or args.id is None or args.id <= 0:
        raise ValueError("нужна ссылка или --peer + положительный --id")
    raw = args.peer.strip().lstrip("@")
    if not re.fullmatch(r"-?[0-9]+|[A-Za-z0-9_]+", raw):
        raise ValueError("--peer должен быть username или числовым ID")
    return int(raw) if raw.lstrip("-").isdigit() else raw, args.id


async def pace():
    await asyncio.sleep(2)


async def download_story(client, peer_name, story_id, out):
    from telethon.tl.functions.stories import (
        GetStoriesByIDRequest, ReadStoriesRequest, IncrementStoryViewsRequest,
    )
    from telethon.tl.types import StoryItem

    await pace()
    peer = await client.get_input_entity(peer_name)
    await pace()
    result = await client(GetStoriesByIDRequest(peer=peer, id=[story_id]))
    story = next((s for s in result.stories
                  if s.id == story_id and isinstance(s, StoryItem)), None)
    if story is None:
        raise ValueError("История недоступна: удалена, истекла или закрыта настройками доступа")
    await pace()
    await client(ReadStoriesRequest(peer=peer, max_id=story_id))
    await pace()
    views_result = await client(IncrementStoryViewsRequest(peer=peer, id=[story_id]))
    owner = next((e for e in [*result.users, *result.chats]
                  if e.id == getattr(peer, "user_id", getattr(peer, "channel_id", None))), None)
    name = getattr(owner, "title", "") or " ".join(filter(None, (
        getattr(owner, "first_name", None), getattr(owner, "last_name", None)))) or str(peer_name)
    print(f"Автор: {name} (@{getattr(owner, 'username', None) or '-'}); история {story_id}")
    print(f"Дата: {story.date.astimezone(timezone.utc).isoformat()}")
    print(f"Тип: {type(story.media).__name__}")
    print(f"Подпись: {story.caption or ''}")
    print(f"Прочтение до {story_id} отмечено; запрос учёта просмотра выполнен (Telegram: {views_result}).")
    out = Path(out).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    await pace()
    path = await client.download_media(story.media, file=str(
        out / f"story-{getattr(owner, 'id', peer_name)}-{story_id}"))
    if not path:
        raise ValueError("У истории нет доступного для скачивания медиа")
    print(f"Сохранено: {path}")
    return path


def settings():
    required = ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_SESSION_FILE",
                "TELEGRAM_FINGERPRINT_FILE", "TELEGRAM_OWNER_ID")
    values = {key: os.environ.get(key) for key in required}
    missing = [key for key, value in values.items() if not value]
    if missing:
        raise ValueError("Не заданы переменные: " + ", ".join(missing))
    try:
        api_id = int(values["TELEGRAM_API_ID"])
        owner_id = int(values["TELEGRAM_OWNER_ID"])
        if api_id <= 0 or owner_id <= 0:
            raise ValueError
    except ValueError:
        raise ValueError("TELEGRAM_API_ID и TELEGRAM_OWNER_ID должны быть положительными числами") from None
    session = Path(values["TELEGRAM_SESSION_FILE"]).expanduser().resolve()
    if session.suffix != ".session" or not session.is_file():
        raise ValueError("Нужен существующий файл .session; новый вход не выполняется")
    fingerprint = json.loads(Path(values["TELEGRAM_FINGERPRINT_FILE"]).expanduser().read_text())
    fields = ("device_model", "system_version", "app_version", "lang_code", "system_lang_code")
    if not isinstance(fingerprint, dict) or any(
        not isinstance(fingerprint.get(key), str) or not fingerprint[key].strip() for key in fields
    ):
        raise ValueError("JSON устройства должен содержать пять непустых сохранённых строк")
    return str(session), api_id, values["TELEGRAM_API_HASH"], owner_id, {k: fingerprint[k] for k in fields}


async def run(args):
    from telethon import TelegramClient

    peer, story_id = target(args)
    session, api_id, api_hash, owner_id, fingerprint = settings()
    lock_fd = os.open(session + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Сессия занята; дождитесь завершения другого процесса") from None
        client = TelegramClient(session, api_id, api_hash, **fingerprint,
                                receive_updates=False, request_retries=0,
                                connection_retries=0, flood_sleep_threshold=0,
                                auto_reconnect=False)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                raise ValueError("Сессия не авторизована; остановитесь, новый вход не выполняется")
            await pace()
            me = await client.get_me()
            if me is None or me.id != owner_id:
                raise ValueError("Владелец сессии не совпал; запрос истории не выполнялся")
            return await download_story(client, peer, story_id, args.out)
        finally:
            await client.disconnect()
    finally:
        os.close(lock_fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("link", nargs="?")
    parser.add_argument("--peer")
    parser.add_argument("--id", type=int)
    parser.add_argument("--out", default="./stories")
    args = parser.parse_args()
    try:
        target(args)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        asyncio.run(run(args))
    except ImportError:
        print("СТОП: нужен Telethon 1.x с модулем stories в этом Python-окружении.", file=sys.stderr)
        return 2
    except Exception as exc:
        # Не печатаем произвольный RPC/сетевой exception: в нём могут быть данные запроса.
        from telethon.errors import FloodWaitError
        if isinstance(exc, FloodWaitError):
            print(f"СТОП: FloodWait {exc.seconds} с; повторов нет. Сообщите владельцу.", file=sys.stderr)
        elif isinstance(exc, ValueError):
            print(f"СТОП: {exc}", file=sys.stderr)
        else:
            print(f"СТОП: {type(exc).__name__}; повторов нет. При FROZEN/auth-ошибке "
                  "прекратите операции с аккаунтом и сообщите владельцу.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
