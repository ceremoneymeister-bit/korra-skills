#!/usr/bin/env python3
"""Эмодзи-паки и стикерпаки Telegram через бота: от источника до превью с номерами.

Команды: setup, whoami, info, identify, fetch, prepare, validate, sheet, create, add,
delete, order, title, cover, emoji, preview, delete-set. Подробности:
`stickerpack.py <команда> --help`. Python ≥ 3.10, только stdlib + Pillow; ffmpeg/ffprobe
нужны лишь для видео (prepare/validate/sheet/preview). Токен бота никогда не печатается.

Лицензия: MIT. Репозиторий: https://github.com/ceremoneymeister-bit/korra-skills
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import html
import json
import mimetypes
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from collections import defaultdict
from pathlib import Path

MUTATING_PAUSE = 0.8
TOKEN_VARS = ("STICKER_BOT_TOKEN", "TELEGRAM_BOT_TOKEN")
FFMPEG_HINT = ("установите ffmpeg: Debian/Ubuntu — apt install ffmpeg, macOS — brew install ffmpeg, "
               "Windows — winget install ffmpeg")

LIMITS = {"emoji": 200, "sticker": 120}
MAX_WEBM_BYTES = 256 * 1024
MAX_STATIC_BYTES = 512 * 1024
MAX_TGS_BYTES = 64 * 1024
COVER_STATIC_BYTES = 128 * 1024
COVER_ANIMATED_BYTES = 32 * 1024
PREVIEW_MARKER = ".sticker-preview"
MAX_DURATION = 3.0
MAX_FPS = 30.0
STATIC_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
VIDEO_EXT = {".gif", ".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}
ALL_EXT = STATIC_EXT | VIDEO_EXT | {".tgs"}
FILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,}$")

MUTATING_METHODS = {
    "createNewStickerSet", "addStickerToSet", "deleteStickerFromSet", "setStickerPositionInSet",
    "setStickerSetTitle", "setStickerSetThumbnail", "setCustomEmojiStickerSetThumbnail",
    "setStickerEmojiList", "setStickerKeywords", "deleteStickerSet", "uploadStickerFile",
}

_SECRETS: set[str] = set()


class CliError(Exception):
    """Ошибка пользователя или API с понятным сообщением (без токена)."""


class MissingToolError(CliError):
    """Не установлен внешний инструмент (ffmpeg/ffprobe)."""


class ApiError(CliError):
    def __init__(self, method: str, description: str, code: int | None = None):
        self.method = method
        self.description = redact(description)
        self.code = code
        super().__init__(f"{method}: {self.description}")


def redact(text: str) -> str:
    text = str(text)
    for s in _SECRETS:
        if s:
            text = text.replace(s, "<токен скрыт>")
    return re.sub(r"bot\d{6,}:[A-Za-z0-9_-]{20,}", "bot<токен скрыт>", text)


# ---------------------------------------------------------------- токен и API

def parse_env(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        out[k.strip()] = v
    return out


def pil():
    try:
        from PIL import Image, ImageOps
    except ImportError:
        raise CliError("нужен Pillow: pip install pillow") from None
    return Image, ImageOps


def env_get(env, name: str) -> str | None:
    """Значение переменной: из отображения env (в тестах) или из окружения процесса."""
    return os.environ.get(name) if env is None else env.get(name)


def home_dir(env=None) -> Path:
    if env_get(env, "STICKERPACKS_HOME"):
        return Path(env_get(env, "STICKERPACKS_HOME")).expanduser()
    if env_get(env, "HERMES_HOME"):
        return Path(env_get(env, "HERMES_HOME")).expanduser() / "sticker-packs"
    return Path.home() / ".sticker-packs"


def load_config(home: Path) -> dict:
    p = Path(home) / "config.json"
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text())
    except (ValueError, OSError):
        raise CliError(f"{p} повреждён: исправьте или удалите файл и запустите setup") from None
    if not isinstance(data, dict):
        raise CliError(f"{p}: ожидается JSON-объект")
    return data


def save_config(home: Path, config: dict) -> Path:
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    p = home / "config.json"
    tmp = home / "config.json.tmp"
    tmp.write_text(json.dumps(config, ensure_ascii=False, indent=1))
    tmp.replace(p)
    return p


NO_TOKEN_HELP = (
    "токен бота не найден. Нужен свой бот для паков:\n"
    "  1. Откройте @BotFather → /newbot, придумайте имя и username (на «bot»).\n"
    "  2. Откройте нового бота в Telegram и нажмите «Старт» (иначе он не сможет писать вам).\n"
    "  3. Положите токен в переменную STICKER_BOT_TOKEN: в Korra — «Настройки → Ключи и доступы → "
    "Пользовательские ключи», в Hermes — файл .env в каталоге Hermes ($HERMES_HOME, обычно ~/.hermes), "
    "в обычной оболочке — export STICKER_BOT_TOKEN=…\n"
    "     Либо укажите файл: --token-file ПУТЬ [--token-var ИМЯ] или `setup --token-env-file ПУТЬ`.\n"
    "  4. Запустите `stickerpack.py setup`.\n"
    "Не присылайте токен в чат: он даёт полный доступ к боту.")


def read_token_file(path: Path, var: str | None = None) -> tuple[str, str | None]:
    """Токен из файла: dotenv (переменная var или STICKER_BOT_TOKEN, затем TELEGRAM_BOT_TOKEN)
    либо файл с одним токеном. Возвращает (токен, имя переменной или None)."""
    try:
        text = Path(path).read_text()
    except OSError as e:
        raise CliError(f"не удалось прочитать токен из {path}: {e.strerror}") from None
    env = parse_env(text)
    for name in ([var] if var else TOKEN_VARS):
        if env.get(name):
            return env[name], name
    if "=" not in text and text.strip():
        return text.strip(), None
    raise CliError(f"в {path} нет переменной {var or ' или '.join(TOKEN_VARS)}")


def resolve_token(token_file: str | None = None, token_var: str | None = None,
                  config: dict | None = None, env=None) -> str:
    """Порядок: --token-file → STICKER_BOT_TOKEN → config.json (token_env_file + token_var)."""
    config = config or {}
    token = None
    env_token = (env_get(env, "STICKER_BOT_TOKEN") or "").strip()
    if token_file:
        token, _ = read_token_file(Path(token_file).expanduser(), token_var)
    elif env_token:
        token = env_token
    elif config.get("token_env_file"):
        token, _ = read_token_file(Path(config["token_env_file"]).expanduser(),
                                   token_var or config.get("token_var"))
    if not token:
        raise CliError(NO_TOKEN_HELP)
    _SECRETS.add(token)
    return token


def resolve_owner(flag: int | None, config: dict | None = None, env=None) -> tuple[int | None, str]:
    """Порядок: --owner → STICKER_OWNER_ID → config.json (owner_id). (None, '') если не задан."""
    if flag is not None:
        return flag, "флаг --owner"
    raw = (env_get(env, "STICKER_OWNER_ID") or "").strip()
    if raw:
        try:
            return int(raw), "STICKER_OWNER_ID"
        except ValueError:
            raise CliError("STICKER_OWNER_ID должна быть числом — Telegram id владельца") from None
    cfg = (config or {}).get("owner_id")
    if cfg is not None:
        try:
            return int(cfg), "config.json"
        except (TypeError, ValueError):
            raise CliError("config.json: owner_id должен быть числом") from None
    return None, ""


OWNER_HINT = ("не задан владелец наборов (Telegram id). Запустите `stickerpack.py setup` "
              "(найдёт вас среди тех, кто написал боту «Старт») или укажите --owner ID / STICKER_OWNER_ID")


class Api:
    def __init__(self, token: str, pause: float = MUTATING_PAUSE):
        self.token = token
        self.pause = pause
        _SECRETS.add(token)

    def _open(self, req, method: str):
        last = "сетевая ошибка"
        for attempt in range(3):
            try:
                return urllib.request.urlopen(req, timeout=90)
            except urllib.error.HTTPError as e:
                return e
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                last = f"сетевая ошибка: {getattr(e, 'reason', e)}"
                time.sleep(2 * (attempt + 1))
        raise ApiError(method, last)

    def call(self, method: str, params: dict | None = None, files: dict | None = None):
        params = params or {}
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        for _ in range(12):
            if files:
                boundary = uuid.uuid4().hex
                body = b""
                for k, v in params.items():
                    v = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
                    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"'
                             f"\r\n\r\n{v}\r\n").encode()
                for k, p in files.items():
                    p = Path(p)
                    ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
                    if p.suffix == ".tgs":
                        ctype = "application/x-tgsticker"
                    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; '
                             f'filename="{p.name}"\r\nContent-Type: {ctype}\r\n\r\n').encode()
                    body += p.read_bytes() + b"\r\n"
                body += f"--{boundary}--\r\n".encode()
                req = urllib.request.Request(
                    url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
            else:
                req = urllib.request.Request(
                    url, data=json.dumps(params, ensure_ascii=False).encode(),
                    headers={"Content-Type": "application/json"})
            resp = self._open(req, method)
            try:
                data = json.loads(resp.read())
            except (ValueError, OSError):
                raise ApiError(method, f"нечитаемый ответ (HTTP {getattr(resp, 'status', '?')})") from None
            if data.get("ok"):
                if method in MUTATING_METHODS:
                    time.sleep(self.pause)
                return data["result"]
            retry = (data.get("parameters") or {}).get("retry_after")
            if retry:
                print(f"  Telegram просит подождать {retry} с, повторяю {method}…", file=sys.stderr)
                time.sleep(retry + 1)
                continue
            raise ApiError(method, data.get("description", "неизвестная ошибка"), data.get("error_code"))
        raise ApiError(method, "слишком много повторов из-за ограничения частоты")

    def download(self, file_id: str) -> tuple[bytes, str]:
        info = self.call("getFile", {"file_id": file_id})
        path = info.get("file_path") if isinstance(info, dict) else None
        if not path:
            raise ApiError("getFile", "Telegram не отдал файл (возможно, больше 20 МБ или временно недоступен)")
        req = urllib.request.Request(f"https://api.telegram.org/file/bot{self.token}/{path}")
        resp = self._open(req, "download")
        if getattr(resp, "status", 200) != 200:
            raise ApiError("download", f"HTTP {getattr(resp, 'status', '?')}")
        return resp.read(), Path(path).suffix.lower()

    def get_set(self, name: str, missing_ok: bool = False):
        try:
            return self.call("getStickerSet", {"name": name})
        except ApiError as e:
            if missing_ok and "STICKERSET_INVALID" in e.description:
                return None
            raise


class Ctx:
    def __init__(self, api, owner: int | None = None, base: Path | None = None,
                 config: dict | None = None, owner_source: str = ""):
        self.api = api
        self._owner = owner
        self.owner_source = owner_source
        self.base = Path(base) if base else home_dir()
        self.config = config or {}
        self._bot = None

    @property
    def owner(self) -> int:
        if self._owner is None:
            raise CliError(OWNER_HINT)
        return self._owner

    @property
    def owner_or_none(self) -> int | None:
        return self._owner

    @property
    def bot(self) -> dict:
        if self._bot is None:
            self._bot = self.api.call("getMe")
        return self._bot

    def full_name(self, short: str) -> str:
        name = normalize_name(short, self.bot["username"])
        validate_name(name)
        return name

    def pack_dir(self, name: str) -> Path:
        return self.base / name

    def get_set(self, name: str):
        """getStickerSet; для короткого имени нашего пака повторяет с суффиксом _by_<бот>."""
        try:
            return self.api.get_set(name)
        except ApiError as e:
            suffix = f"_by_{self.bot['username']}"
            if "STICKERSET_INVALID" not in e.description or name.lower().endswith(suffix.lower()):
                raise
            try:
                return self.api.get_set(name + suffix)
            except ApiError:
                raise e from None


# ------------------------------------------------------------------ разбор

def normalize_name(short: str, bot_username: str) -> str:
    short = short.strip()
    suffix = f"_by_{bot_username}"
    if short.lower().endswith(suffix.lower()):
        return short
    return short + suffix


def validate_name(name: str) -> None:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name):
        raise CliError("короткое имя: только латиница, цифры и «_», начинается с буквы")
    if "__" in name:
        raise CliError("короткое имя не должно содержать «__»")
    if len(name) > 64:
        raise CliError(f"короткое имя длиннее 64 символов ({len(name)})")


def validate_title(title: str) -> None:
    if not 1 <= len(title) <= 64:
        raise CliError(f"название должно быть от 1 до 64 символов (сейчас {len(title)})")


def parse_positions(spec: str, n: int, what: str = "позиции") -> list[int]:
    """«1,3,5-9» → [1,3,5,6,7,8,9]. Нумерация с 1; повтор и выход за 1..n — ошибка.
    Диапазон «9-5» разворачивается по убыванию."""
    out: list[int] = []
    seen: set[int] = set()
    parts = [p for p in re.split(r"[,\s;]+", spec.strip()) if p]
    if not parts:
        raise CliError(f"{what}: пустое значение")
    for part in parts:
        m = re.fullmatch(r"(\d+)(?:[-–](\d+))?", part)
        if not m:
            raise CliError(f"{what}: не понимаю «{part}» (ожидается число или диапазон 5-9)")
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        rng = range(a, b + 1) if a <= b else range(a, b - 1, -1)
        for x in rng:
            if not 1 <= x <= n:
                raise CliError(f"{what}: {x} вне диапазона 1..{n}")
            if x in seen:
                raise CliError(f"{what}: {x} указана повторно")
            seen.add(x)
            out.append(x)
    return out


def compress_ranges(nums: list[int]) -> str:
    nums = sorted(nums)
    parts, i = [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        parts.append(str(nums[i]) if i == j else f"{nums[i]}-{nums[j]}")
        i = j + 1
    return ",".join(parts)


def resolve_order(spec: str, n: int) -> tuple[list[int], list[int]]:
    """Возвращает (полный порядок позиций 1..n, не упомянутые в конце)."""
    given = parse_positions(spec, n, "порядок")
    rest = [i for i in range(1, n + 1) if i not in set(given)]
    return given + rest, rest


def parse_set_ref(ref: str) -> tuple[str, str | None]:
    ref = ref.strip()
    ref = re.sub(r"^https?://t\.me/(addemoji|addstickers)/", "", ref)
    name, _, pick = ref.partition(":")
    if not name:
        raise CliError("пустое имя набора")
    return name, (pick or None)


def plan_moves(current: list[int], target: list[int]) -> list[tuple[int, int]]:
    """Минимальный набор перемещений (элемент, новая позиция с 0), приводящий
    current к target. Элементы вне наибольшей общей возрастающей подпоследовательности
    перемещаются сразу после своего предшественника в target."""
    if sorted(current) != sorted(target):
        raise CliError("внутренняя ошибка: состав порядков не совпадает")
    idx = {x: i for i, x in enumerate(current)}
    seq = [idx[x] for x in target]
    n = len(seq)
    best = [1] * n
    prev = [-1] * n
    for i in range(n):
        for j in range(i):
            if seq[j] < seq[i] and best[j] + 1 > best[i]:
                best[i], prev[i] = best[j] + 1, j
    keep: set[int] = set()
    if n:
        k = max(range(n), key=lambda i: best[i])
        while k != -1:
            keep.add(target[k])
            k = prev[k]
    cur = list(current)
    moves: list[tuple[int, int]] = []
    for i, x in enumerate(target):
        if x in keep:
            continue
        cur.remove(x)
        pos = 0 if i == 0 else cur.index(target[i - 1]) + 1
        cur.insert(pos, x)
        moves.append((x, pos))
    if cur != list(target):
        raise CliError("внутренняя ошибка: план перестановки не сошёлся")
    return moves


# ---------------------------------------------------------------- file_id

def _rle_decode(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(data):
        if data[i] == 0:
            if i + 1 >= len(data):
                raise ValueError("оборванная RLE-последовательность")
            out += b"\x00" * data[i + 1]
            i += 2
        else:
            out.append(data[i])
            i += 1
    return bytes(out)


def decode_file_id(file_id: str) -> dict:
    """Bot API file_id → {type, dc_id, id, access_hash}. Для custom emoji id == custom_emoji_id."""
    try:
        raw = base64.urlsafe_b64decode(file_id + "=" * (-len(file_id) % 4))
        data = _rle_decode(raw)
        major = data[-1]
        body = data[:-2] if major >= 4 else data[:-1]
        ftype, dc = struct.unpack("<ii", body[:8])
        off = 8
        if ftype & (1 << 25):
            ln = body[off]
            head = 1
            if ln == 254:
                ln = int.from_bytes(body[off + 1:off + 4], "little")
                head = 4
            total = head + ln
            off += total + (-total) % 4
        doc_id, access_hash = struct.unpack("<qq", body[off:off + 16])
    except (ValueError, struct.error, IndexError):
        raise CliError(f"не удалось разобрать file_id «{file_id[:24]}…»") from None
    return {"type": ftype & ~((1 << 25) | (1 << 24)), "dc_id": dc, "id": doc_id, "access_hash": access_hash}


# --------------------------------------------------------- ffprobe / проверки

def probe(path: Path, count_frames: bool = False) -> dict:
    cmd = ["ffprobe", "-v", "error", "-show_entries",
           "stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,nb_read_frames"
           ":format=duration", "-of", "json"]
    if count_frames:
        cmd.insert(3, "-count_frames")
    try:
        out = subprocess.check_output(cmd + [str(path)], stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        raise CliError(f"ffprobe не смог прочитать {path.name}: {e.stderr.decode(errors='ignore')[:200]}") from None
    except FileNotFoundError:
        raise MissingToolError(f"ffprobe не найден — {FFMPEG_HINT}") from None
    return json.loads(out)


def _rate(s: str | None) -> float:
    try:
        a, b = (s or "0/1").split("/")
        return float(a) / float(b) if float(b) else 0.0
    except ValueError:
        return 0.0


def video_facts(path: Path) -> dict:
    info = probe(path, count_frames=True)
    streams = info.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    try:
        dur = float(info.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        dur = None
    frames = int(v.get("nb_read_frames") or 0)
    fps = _rate(v.get("avg_frame_rate")) or _rate(v.get("r_frame_rate"))
    if fps <= 0 and dur:
        fps = frames / dur
    if dur is None and fps > 0:
        dur = frames / fps
    return {"streams": [s.get("codec_type") for s in streams], "codec": v.get("codec_name"),
            "width": v.get("width"), "height": v.get("height"), "duration": dur, "fps": fps,
            "frames": frames}


def format_of(path: Path) -> str:
    ext = path.suffix.lower()
    return "animated" if ext == ".tgs" else "video" if ext == ".webm" else "static"


def dims_ok(w: int, h: int, kind: str) -> bool:
    if kind == "emoji":
        return w == 100 and h == 100
    return max(w, h) == 512 and min(w, h) <= 512


def dims_rule(kind: str) -> str:
    return "ровно 100×100" if kind == "emoji" else "одна сторона ровно 512, другая ≤512"


def validate_file(path: Path, kind: str) -> list[str]:
    """Список нарушений требований Telegram (пустой — файл подходит)."""
    path = Path(path)
    ext = path.suffix.lower()
    if not path.is_file():
        return ["файл не найден"]
    size = path.stat().st_size
    bad: list[str] = []
    if ext == ".tgs":
        with open(path, "rb") as f:
            if f.read(2) != b"\x1f\x8b":
                bad.append("TGS должен быть gzip-архивом")
        if size > MAX_TGS_BYTES:
            bad.append(f"TGS {size // 1024} КБ > 64 КБ")
    elif ext == ".webm":
        f = video_facts(path)
        if f["codec"] != "vp9":
            bad.append(f"кодек {f['codec']}, нужен VP9")
        if f["streams"].count("video") != 1 or any(s != "video" for s in f["streams"]):
            bad.append("в файле должен быть один видеопоток и без звука/других потоков")
        if not f["width"] or not dims_ok(f["width"], f["height"], kind):
            bad.append(f"размер {f['width']}×{f['height']}, нужно {dims_rule(kind)}")
        if f["duration"] is None or f["duration"] > MAX_DURATION + 0.001:
            bad.append(f"длительность {f['duration']} с > 3 с")
        if f["fps"] > MAX_FPS + 0.01:
            bad.append(f"{f['fps']:.1f} кадров/с > 30")
        if size > MAX_WEBM_BYTES:
            bad.append(f"размер {size // 1024} КБ > 256 КБ")
    elif ext in {".webp", ".png"}:
        Image, _ = pil()
        try:
            with Image.open(path) as im:
                w, h = im.size
                if getattr(im, "n_frames", 1) > 1:
                    bad.append("анимированный WEBP/PNG не принимается — преобразуйте через prepare в WEBM")
        except Exception as e:  # noqa: BLE001
            return [f"не удалось открыть изображение: {e}"]
        if not dims_ok(w, h, kind):
            bad.append(f"размер {w}×{h}, нужно {dims_rule(kind)}")
        if size > MAX_STATIC_BYTES:
            bad.append(f"размер {size // 1024} КБ > 512 КБ")
    else:
        bad.append(f"формат {ext or 'без расширения'} не принимается Telegram (нужны webp/png/webm/tgs; "
                   "остальное — через prepare)")
    return bad


def validate_cover(path: Path) -> list[str]:
    """Нарушения требований к обложке обычного стикерпака (пустой список — подходит)."""
    ext = path.suffix.lower()
    size = path.stat().st_size
    bad: list[str] = []
    if ext in {".webp", ".png"}:
        Image, _ = pil()
        try:
            with Image.open(path) as im:
                w, h = im.size
                if getattr(im, "n_frames", 1) > 1:
                    bad.append("анимированный WEBP/PNG не подходит: нужна статичная картинка")
        except Exception as e:  # noqa: BLE001
            return [f"не удалось открыть изображение: {e}"]
        if (w, h) != (100, 100):
            bad.append(f"размер {w}×{h}, нужно ровно 100×100")
        if size > COVER_STATIC_BYTES:
            bad.append(f"размер {size / 1024:.0f} КБ > 128 КБ")
    elif ext == ".tgs":
        with open(path, "rb") as f:
            if f.read(2) != b"\x1f\x8b":
                bad.append("TGS должен быть gzip-архивом")
        if size > COVER_ANIMATED_BYTES:
            bad.append(f"TGS {size / 1024:.0f} КБ > 32 КБ")
    elif ext == ".webm":
        f = video_facts(path)
        if f["codec"] != "vp9":
            bad.append(f"кодек {f['codec']}, нужен VP9")
        if (f["width"], f["height"]) != (100, 100):
            bad.append(f"размер {f['width']}×{f['height']}, нужно ровно 100×100")
        if size > COVER_ANIMATED_BYTES:
            bad.append(f"размер {size / 1024:.0f} КБ > 32 КБ")
    else:
        bad.append(f"формат {ext or 'без расширения'} не подходит (нужны WEBP/PNG, TGS или WEBM)")
    return bad


# ------------------------------------------------------------------ prepare

def _target_size(w: int, h: int, kind: str, pad: int = 0) -> tuple[int, int]:
    if kind == "emoji":
        box = 100 - 2 * pad
        s = min(box / w, box / h)
    else:
        s = (512 - 2 * pad) / max(w, h)
    return max(1, round(w * s)), max(1, round(h * s))


ALPHA_TRIM_MAX = 8
DEFAULT_PAD = {"emoji": 2, "sticker": 8}


def _check_pad(pad: int, kind: str) -> None:
    limit = 50 if kind == "emoji" else 256
    if not 0 <= pad < limit:
        raise CliError(f"--pad: от 0 до {limit - 1} пикселей для {kind}")


def prepare_static(src: Path, out: Path, kind: str, trim: bool = True,
                   pad: int | None = None) -> tuple[tuple[int, int], tuple[int, int]]:
    """Возвращает (исходный размер, размер после обрезки)."""
    Image, ImageOps = pil()
    if pad is None:
        pad = DEFAULT_PAD[kind] if trim else 0
    _check_pad(pad, kind)
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im).convert("RGBA")
    orig = im.size
    if trim:
        mask = im.getchannel("A").point(lambda v: 255 if v > ALPHA_TRIM_MAX else 0)
        box = mask.getbbox()
        if box is None:
            raise CliError(f"{src.name}: картинка полностью прозрачная — нечего подготавливать")
        im = im.crop(box)
    trimmed = im.size
    w, h = _target_size(*im.size, kind, pad)
    im = im.resize((w, h), Image.LANCZOS)
    if kind == "emoji":
        canvas = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
        canvas.paste(im, ((100 - w) // 2, (100 - h) // 2))
        im = canvas
    else:
        if pad:
            canvas = Image.new("RGBA", (w + 2 * pad, h + 2 * pad), (0, 0, 0, 0))
            canvas.paste(im, (pad, pad))
            im = canvas
        if max(im.size) != 512:
            raise CliError(f"{src.name}: внутренняя ошибка масштабирования")
    for q in (92, 80, 68, 55, 40):
        im.save(out, "WEBP", quality=q, method=6)
        if out.stat().st_size <= MAX_STATIC_BYTES:
            return orig, trimmed
    raise CliError(f"{src.name}: не удалось уложиться в 512 КБ")


def _frames_from_pillow(src: Path, tmp: Path) -> tuple[Path, float]:
    Image, ImageOps = pil()
    durations = []
    with Image.open(src) as im:
        n = im.n_frames
        for i in range(n):
            im.seek(i)
            durations.append(im.info.get("duration", 40) or 40)
            ImageOps.exif_transpose(im.convert("RGBA")).save(tmp / f"f_{i:05d}.png")
    avg = sum(durations) / len(durations)
    return tmp / "f_%05d.png", min(MAX_FPS, 1000.0 / max(avg, 1))


def prepare_animated(src: Path, out: Path, kind: str) -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        in_args: list[str]
        src_fps: float
        ext = src.suffix.lower()
        if ext in {".webp", ".png"}:
            pattern, fps = _frames_from_pillow(src, tmp)
            in_args = ["-framerate", f"{fps:.3f}", "-i", str(pattern)]
            src_fps = fps
            w, h = _first_frame_size(tmp)
        else:
            info = probe(src)
            v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
            if not v:
                raise CliError(f"{src.name}: нет видеопотока")
            in_args = ["-i", str(src)]
            src_fps = _rate(v.get("avg_frame_rate")) or _rate(v.get("r_frame_rate"))
            w, h = v["width"], v["height"]
        tw, th = _target_size(w, h, kind)
        scale = f"scale={tw}:{th}:flags=lanczos"
        if kind == "emoji":
            vf_tail = f"{scale},pad=100:100:({100 - tw}/2):({100 - th}/2):color=black@0"
        else:
            vf_tail = scale
        stages = [(30, 30), (30, 38), (30, 46), (30, 54), (24, 56), (15, 58), (12, 63)]
        for cap, crf in stages:
            fps_filter = f"fps={cap}," if src_fps > cap + 0.01 else ""
            vf = f"{fps_filter}format=rgba,{vf_tail},format=yuva420p"
            cmd = ["ffmpeg", "-v", "error", "-y", *in_args, "-t", "3", "-an", "-sn", "-vf", vf,
                   "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-b:v", "0", "-crf", str(crf),
                   "-auto-alt-ref", "0", "-deadline", "good", "-cpu-used", "4", "-row-mt", "1",
                   str(out)]
            try:
                subprocess.run(cmd, check=True, capture_output=True)
            except subprocess.CalledProcessError as e:
                raise CliError(f"{src.name}: ffmpeg завершился с ошибкой: "
                               f"{e.stderr.decode(errors='ignore').strip()[-300:]}") from None
            except FileNotFoundError:
                raise MissingToolError(f"ffmpeg не найден — {FFMPEG_HINT}") from None
            if out.stat().st_size <= MAX_WEBM_BYTES:
                return
        raise CliError(f"{src.name}: не удалось уложиться в 256 КБ даже при максимальном сжатии")


def _first_frame_size(tmp: Path) -> tuple[int, int]:
    Image, _ = pil()
    with Image.open(sorted(tmp.glob("f_*.png"))[0]) as im:
        return im.size


def is_animated_source(src: Path) -> bool:
    ext = src.suffix.lower()
    if ext in {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}:
        return True
    if ext in {".gif", ".webp", ".png"}:
        Image, _ = pil()
        with Image.open(src) as im:
            return getattr(im, "n_frames", 1) > 1
    return False


def load_meta_emoji(directory: Path) -> dict[str, str]:
    meta = directory / "meta.json"
    if not meta.is_file():
        return {}
    try:
        items = json.loads(meta.read_text()).get("items", [])
    except (ValueError, OSError):
        return {}
    return {it["file"]: it["emoji"] for it in items if it.get("file") and it.get("emoji")}


def unique_path(directory: Path, stem: str, ext: str, used: set) -> Path:
    p = directory / f"{stem}{ext}"
    n = 2
    while p in used:
        p = directory / f"{stem}_{n}{ext}"
        n += 1
    used.add(p)
    return p


def human_size(n: int) -> str:
    return f"{n / 1024:.1f} КБ" if n < 1024 * 1024 else f"{n / 1048576:.2f} МБ"


def cmd_prepare(ctx, a) -> int:
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    rows, failed, meta_items, used = [], 0, [], set()
    trim = not a.no_trim
    for f in a.files:
        src = Path(f)
        orig_txt = trim_txt = "—"
        try:
            if not src.is_file():
                raise CliError(f"{src.name}: файл не найден")
            ext = src.suffix.lower()
            if ext == ".tgs":
                dst = unique_path(out, src.stem, ".tgs", used)
                bad = validate_file(src, a.kind)
                if bad:
                    raise CliError(f"{src.name}: " + "; ".join(bad))
                shutil.copy2(src, dst)
            elif ext not in ALL_EXT:
                raise CliError(f"{src.name}: неподдерживаемый формат {ext or '(нет расширения)'}")
            elif ext == ".webm" and not validate_file(src, a.kind):
                dst = unique_path(out, src.stem, ".webm", used)
                shutil.copy2(src, dst)
            elif is_animated_source(src):
                dst = unique_path(out, src.stem, ".webm", used)
                prepare_animated(src, dst, a.kind)
            else:
                dst = unique_path(out, src.stem, ".webp", used)
                orig, trimmed = prepare_static(src, dst, a.kind, trim=trim, pad=a.pad)
                orig_txt = "×".join(map(str, orig))
                trim_txt = "×".join(map(str, trimmed)) if trim else "не обрезалось"
            bad = validate_file(dst, a.kind)
            if bad:
                raise CliError(f"{src.name}: результат не прошёл проверку: " + "; ".join(bad))
            dur = ""
            res_txt = "—"
            if dst.suffix == ".webm":
                dur = f"{video_facts(dst)['duration']:.2f} с"
            if dst.suffix == ".webp":
                Image, _ = pil()
                with Image.open(dst) as im:
                    res_txt = "×".join(map(str, im.size))
            rows.append((src.name, dst.name, human_size(dst.stat().st_size), dur or "—",
                         orig_txt, trim_txt, res_txt))
            emoji = load_meta_emoji(src.parent).get(src.name)
            if emoji:
                meta_items.append({"file": dst.name, "emoji": emoji})
        except CliError as e:
            failed += 1
            rows.append((src.name, f"ОШИБКА: {e}", "", "", "", "", ""))
    w = max((len(r[0]) for r in rows), default=8)
    print(f"{'исходник':<{w}}  | исходный размер | после обрезки | итог | → результат  | размер | длительность")
    for r in rows:
        if r[4] == "":
            print(f"{r[0]:<{w}}  → {r[1]}")
        else:
            print(f"{r[0]:<{w}}  | {r[4]} | {r[5]} | {r[6]} | → {r[1]}  | {r[2]} | {r[3]}")
    if meta_items:
        (out / "meta.json").write_text(json.dumps({"items": meta_items}, ensure_ascii=False, indent=1))
    print(f"\nготово: {len(rows) - failed} из {len(rows)} в {out}")
    return 1 if failed else 0


def cmd_validate(ctx, a) -> int:
    bad_total = 0
    for f in a.files:
        bad = validate_file(Path(f), a.kind)
        if bad:
            bad_total += 1
            print(f"✗ {Path(f).name}")
            for b in bad:
                print(f"    — {b}")
        else:
            print(f"✓ {Path(f).name}")
    print(f"\nнарушений: {bad_total} файл(ов) из {len(a.files)}")
    return 1 if bad_total else 0


# ------------------------------------------------------------------ состояние

def remote_kind(remote: dict) -> str:
    return "emoji" if remote.get("sticker_type") == "custom_emoji" else "sticker"


def remote_format(st: dict) -> str:
    return "animated" if st.get("is_animated") else "video" if st.get("is_video") else "static"


def remote_key(st: dict, kind: str) -> str:
    return st.get("custom_emoji_id") if kind == "emoji" and st.get("custom_emoji_id") else st["file_unique_id"]


def item_key(it: dict) -> str | None:
    return it.get("custom_emoji_id") or it.get("file_unique_id")


def state_path(ctx, name: str) -> Path:
    return ctx.pack_dir(name) / "pack.json"


def load_state(ctx, name: str) -> dict | None:
    p = state_path(ctx, name)
    if not p.is_file():
        return None
    try:
        state = json.loads(p.read_text())
    except ValueError:
        raise CliError(f"{p} повреждён; удалите его, состояние будет восстановлено по Telegram") from None
    if "stand_dir" in state:
        legacy = state.pop("stand_dir")
        state.setdefault("preview_name", legacy)
    return state


def save_state(ctx, name: str, state: dict) -> None:
    d = ctx.pack_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "pack.json.tmp"
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    tmp.replace(d / "pack.json")


def sync_state(state: dict | None, remote: dict, adopt: list[dict] | None = None,
               expected_removed: int = 0) -> tuple[dict, list[str]]:
    """Порядок и состав — по факту из Telegram; src/source/src_md5 сохраняются по ключу."""
    warnings: list[str] = []
    kind = remote_kind(remote)
    fresh = state is None
    state = state or {"items": []}
    old: dict[str, list[dict]] = defaultdict(list)
    for it in state.get("items", []):
        old[item_key(it)].append(it)
    items: list[dict] = []
    new_idx: list[int] = []
    for st in remote["stickers"]:
        k = remote_key(st, kind)
        if old.get(k):
            it = old[k].pop(0)
            it["emoji"], it["format"] = st.get("emoji", it.get("emoji")), remote_format(st)
        else:
            it = {"emoji": st.get("emoji"), "format": remote_format(st), "src": None, "source": None,
                  "file_unique_id": st["file_unique_id"]}
            if kind == "emoji":
                it["custom_emoji_id"] = st.get("custom_emoji_id")
            new_idx.append(len(items))
        items.append(it)
    adopt = list(adopt or [])
    for i, meta in zip(new_idx[len(new_idx) - len(adopt):] if adopt else [], adopt):
        items[i].update(meta)
    unknown = len(new_idx) - len(adopt)
    missing = sum(len(v) for v in old.values())
    if fresh:
        if unknown > 0:
            warnings.append("pack.json не было — создан по фактическому содержимому набора, "
                            f"src неизвестен у {unknown} элемент(ов)")
    else:
        if unknown > 0:
            warnings.append(f"{unknown} элемент(ов) добавлено вне скрипта — src помечен null")
        if missing > expected_removed:
            warnings.append(f"{missing - expected_removed} элемент(ов) исчезло вне скрипта")
    state.update({"name": remote["name"], "title": remote["title"], "kind": kind, "items": items})
    return state, warnings


def refresh(ctx, name: str, adopt=None, expected_removed: int = 0, quiet: bool = False):
    remote = ctx.api.get_set(name)
    state, warnings = sync_state(load_state(ctx, name), remote, adopt, expected_removed)
    save_state(ctx, name, state)
    if not quiet:
        for w in warnings:
            print(f"  ⚠ {w}", file=sys.stderr)
    return remote, state


# ----------------------------------------------------------------- источники

def md5_of(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


class Src:
    def __init__(self, path: Path, emoji: str, source: str):
        self.path, self.emoji, self.source = Path(path), emoji, source
        self.fmt = format_of(self.path)
        self.md5 = md5_of(self.path)

    def meta(self) -> dict:
        return {"src": str(self.path.resolve()), "source": self.source, "src_md5": self.md5}


def emoji_list(spec: str) -> list[str]:
    parts = [p for p in re.split(r"[,\s]+", spec.strip()) if p]
    if not parts:
        raise CliError("пустой emoji")
    return parts


def natural_key(p: Path):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", p.name)]


def download_set(ctx, ref: str, files_dir: Path) -> list[Src]:
    name, pick = parse_set_ref(ref)
    remote = ctx.get_set(name)
    name = remote["name"]
    stickers = remote["stickers"]
    positions = parse_positions(pick, len(stickers), f"выбор из «{name}»") if pick else list(
        range(1, len(stickers) + 1))
    files_dir.mkdir(parents=True, exist_ok=True)
    srcs = []
    for pos in positions:
        st = stickers[pos - 1]
        data, ext = ctx.api.download(st["file_id"])
        path = files_dir / f"{name}_{pos:03d}{ext}"
        path.write_bytes(data)
        srcs.append(Src(path, st.get("emoji") or "⭐", f"set:{name}#{pos}"))
    return srcs


def resolve_sources(ctx, sources: list, default_emoji: str, files_dir: Path) -> list[Src]:
    out: list[Src] = []
    for kind, value in sources:
        if kind == "files":
            for f in value:
                p = Path(f)
                if not p.is_file():
                    raise CliError(f"файл не найден: {p}")
                emoji = load_meta_emoji(p.parent).get(p.name) or default_emoji
                out.append(Src(p, emoji, f"file:{p.name}"))
        elif kind == "dir":
            d = Path(value)
            if not d.is_dir():
                raise CliError(f"каталог не найден: {d}")
            meta = load_meta_emoji(d)
            for p in sorted((x for x in d.iterdir() if x.suffix.lower() in ALL_EXT), key=natural_key):
                out.append(Src(p, meta.get(p.name) or default_emoji, f"file:{p.name}"))
        else:
            out.extend(download_set(ctx, value, files_dir))
    if not out:
        raise CliError("не указано ни одного источника (--files / --from-dir / --from-set)")
    return out


def pending_sources(state_items: list[dict], srcs: list[Src]) -> list[Src]:
    have: dict[str, int] = defaultdict(int)
    for it in state_items:
        if it.get("src_md5"):
            have[it["src_md5"]] += 1
    seen: dict[str, int] = defaultdict(int)
    todo = []
    for s in srcs:
        seen[s.md5] += 1
        if seen[s.md5] > have[s.md5]:
            todo.append(s)
    return todo


def check_sources(srcs: list[Src], kind: str) -> None:
    problems = []
    for s in srcs:
        for b in validate_file(s.path, kind):
            problems.append(f"  {s.path.name}: {b}")
    if problems:
        raise CliError("файлы не подходят под требования Telegram (приведите через prepare):\n"
                       + "\n".join(problems[:30]) + (f"\n  …ещё {len(problems) - 30}" if len(problems) > 30 else ""))


def push_sources(ctx, name: str, kind: str, todo: list[Src], create: dict | None) -> int:
    """Загружает и добавляет по одному; после каждого сверяет состояние с Telegram."""
    created = create is None
    for n, s in enumerate(todo, 1):
        up = ctx.api.call("uploadStickerFile", {"user_id": ctx.owner, "sticker_format": s.fmt},
                          files={"sticker": s.path})
        sticker = {"sticker": up["file_id"], "format": s.fmt, "emoji_list": emoji_list(s.emoji)}
        if not created:
            params = {"user_id": ctx.owner, "name": name, "title": create["title"],
                      "sticker_type": "custom_emoji" if kind == "emoji" else "regular",
                      "stickers": [sticker]}
            if create.get("repaint"):
                params["needs_repainting"] = True
            ctx.api.call("createNewStickerSet", params)
            created = True
        else:
            ctx.api.call("addStickerToSet", {"user_id": ctx.owner, "name": name, "sticker": sticker})
        refresh(ctx, name, adopt=[s.meta()])
        print(f"  [{n}/{len(todo)}] {s.path.name} {s.emoji} добавлен")
    return len(todo)


def cmd_create(ctx, a) -> int:
    name = ctx.full_name(a.short_name)
    validate_title(a.title)
    kind = a.kind
    if a.repaint and kind != "emoji":
        raise CliError("--repaint применим только к эмодзи-пакам")
    dry_td = tempfile.TemporaryDirectory() if a.dry_run else None
    try:
        files_dir = Path(dry_td.name) if dry_td else ctx.pack_dir(name) / "files"
        srcs = resolve_sources(ctx, a.sources, a.emoji, files_dir)
        check_sources(srcs, kind)
        remote = ctx.api.get_set(name, missing_ok=True)
        state = load_state(ctx, name) if remote else None
        if remote:
            if remote_kind(remote) != kind:
                raise CliError(f"набор {name} уже существует и имеет другой тип ({remote_kind(remote)})")
            if state is None and remote["stickers"]:
                raise CliError(f"набор {name} уже существует ({len(remote['stickers'])} шт.), но pack.json нет — "
                               "чтобы не задвоить, используйте add (он не пропускает дубли без pack.json)")
        todo = pending_sources(state["items"] if state else [], srcs)
        total = (len(remote["stickers"]) if remote else 0) + len(todo)
        if total > LIMITS[kind]:
            raise CliError(f"лимит {LIMITS[kind]} элементов будет превышен: {total}")
        status = "уже существует, продолжаю" if remote else "имя свободно"
        print(f"{name} «{a.title}» ({kind}): {status}; источников {len(srcs)}, к добавлению {len(todo)}")
        if a.dry_run:
            for i, s in enumerate(srcs, 1):
                print(f"  {i:>3}. {s.path.name} {s.emoji} {s.fmt}")
            print("dry-run: ничего не создано")
            return 0
        if not todo:
            print("всё уже добавлено")
            return 0
        push_sources(ctx, name, kind, todo, None if remote else {"title": a.title, "repaint": a.repaint})
        remote, _ = refresh(ctx, name)
        print(f"готово: {len(remote['stickers'])} шт., https://t.me/{'addemoji' if kind == 'emoji' else 'addstickers'}/{name}")
        return 0
    finally:
        if dry_td:
            dry_td.cleanup()


def cmd_add(ctx, a) -> int:
    name = ctx.full_name(a.short_name)
    remote = ctx.api.get_set(name)
    kind = remote_kind(remote)
    state = load_state(ctx, name)
    if state is None:
        remote, state = refresh(ctx, name)
    srcs = resolve_sources(ctx, a.sources, a.emoji, ctx.pack_dir(name) / "files")
    check_sources(srcs, kind)
    todo = pending_sources(state["items"], srcs)
    before = len(remote["stickers"])
    if before + len(todo) > LIMITS[kind]:
        raise CliError(f"лимит {LIMITS[kind]} элементов будет превышен: {before + len(todo)}")
    if a.at is not None and not 1 <= a.at <= before + 1:
        raise CliError(f"--at вне диапазона 1..{before + 1}")
    if not todo:
        print("всё уже добавлено")
        return 0
    push_sources(ctx, name, kind, todo, None)
    remote, _ = refresh(ctx, name)
    if a.at is not None and a.at != before + 1:
        n = len(remote["stickers"])
        old = list(range(before))
        new = list(range(before, n))
        target = old[:a.at - 1] + new + old[a.at - 1:]
        apply_target(ctx, name, remote, target)
    print(f"готово: добавлено {len(todo)}, в наборе {len(remote['stickers'])}")
    return 0


def apply_target(ctx, name: str, remote: dict, target: list[int]) -> None:
    """target — исходные индексы (с 0) в желаемом порядке."""
    moves = plan_moves(list(range(len(remote["stickers"]))), target)
    for i, (idx, pos) in enumerate(moves, 1):
        ctx.api.call("setStickerPositionInSet", {"sticker": remote["stickers"][idx]["file_id"], "position": pos})
        print(f"  перемещение {i}/{len(moves)}: {idx + 1} → {pos + 1}")
    after, _ = refresh(ctx, name)
    kind = remote_kind(after)
    want = [remote_key(remote["stickers"][i], kind) for i in target]
    got = [remote_key(s, kind) for s in after["stickers"]]
    if want != got:
        raise CliError("порядок в Telegram не совпал с ожидаемым после перестановки; проверьте `info`")
    if not moves:
        print("порядок уже такой")


def cmd_delete(ctx, a) -> int:
    name = ctx.full_name(a.short_name)
    remote = ctx.api.get_set(name)
    positions = parse_positions(a.pos, len(remote["stickers"]), "позиции для удаления")
    ids = [remote["stickers"][p - 1]["file_id"] for p in positions]
    for p, fid in zip(positions, ids):
        ctx.api.call("deleteStickerFromSet", {"sticker": fid})
        print(f"  удалена позиция {p}")
    refresh(ctx, name, expected_removed=len(ids))
    print(f"готово: удалено {len(ids)}")
    return 0


def cmd_order(ctx, a) -> int:
    name = ctx.full_name(a.short_name)
    remote = ctx.api.get_set(name)
    n = len(remote["stickers"])
    full, rest = resolve_order(a.order, n)
    if rest:
        print(f"  ⚠ не упомянуты позиции {compress_ranges(rest)} — поставлены в конец в прежнем порядке",
              file=sys.stderr)
    apply_target(ctx, name, remote, [p - 1 for p in full])
    print("порядок обновлён")
    return 0


def cmd_title(ctx, a) -> int:
    name = ctx.full_name(a.short_name)
    validate_title(a.new_title)
    ctx.api.call("setStickerSetTitle", {"name": name, "title": a.new_title})
    refresh(ctx, name)
    print(f"название: «{a.new_title}»")
    return 0


def cmd_cover(ctx, a) -> int:
    name = ctx.full_name(a.short_name)
    remote = ctx.api.get_set(name)
    if remote_kind(remote) == "emoji":
        if a.file:
            raise CliError("для эмодзи-пака --file не принимается: обложкой служит один из его элементов, "
                           "укажите --pos N")
        if a.pos is None:
            raise CliError("для эмодзи-пака укажите --pos N (позиция эмодзи, которое станет обложкой)")
        if not 1 <= a.pos <= len(remote["stickers"]):
            raise CliError(f"--pos вне диапазона 1..{len(remote['stickers'])}")
        cid = remote["stickers"][a.pos - 1].get("custom_emoji_id")
        ctx.api.call("setCustomEmojiStickerSetThumbnail", {"name": name, "custom_emoji_id": cid})
        print(f"обложка: эмодзи на позиции {a.pos}")
        return 0
    if not a.file:
        raise CliError("для обычного стикерпака обложкой служит отдельный файл: укажите --file "
                       "(WEBP/PNG 100×100 ≤128 КБ, TGS ≤32 КБ или WEBM 100×100 ≤32 КБ)")
    p = Path(a.file)
    if not p.is_file():
        raise CliError(f"файл не найден: {p}")
    bad = validate_cover(p)
    if bad:
        raise CliError("файл не подходит для обложки стикерпака:\n  - " + "\n  - ".join(bad))
    fmt = format_of(p)
    ctx.api.call("setStickerSetThumbnail", {"name": name, "user_id": ctx.owner, "format": fmt},
                 files={"thumbnail": p})
    print("обложка установлена")
    return 0


def cmd_emoji(ctx, a) -> int:
    name = ctx.full_name(a.short_name)
    remote = ctx.api.get_set(name)
    if not 1 <= a.pos <= len(remote["stickers"]):
        raise CliError(f"--pos вне диапазона 1..{len(remote['stickers'])}")
    fid = remote["stickers"][a.pos - 1]["file_id"]
    ctx.api.call("setStickerEmojiList", {"sticker": fid, "emoji_list": emoji_list(a.emoji)})
    if a.keywords is not None:
        kws = [k.strip() for k in a.keywords.split(",") if k.strip()]
        ctx.api.call("setStickerKeywords", {"sticker": fid, "keywords": kws})
    refresh(ctx, name)
    print(f"позиция {a.pos}: emoji {a.emoji}" + (f", ключевые слова {a.keywords}" if a.keywords else ""))
    return 0


def cmd_delete_set(ctx, a) -> int:
    name = ctx.full_name(a.short_name)
    if a.confirm != name:
        raise CliError(f"подтверждение не совпало: нужно --confirm {name}")
    ctx.api.call("deleteStickerSet", {"name": name})
    p = state_path(ctx, name)
    if p.is_file():
        p.rename(p.with_name("pack.deleted.json"))
    print(f"набор {name} удалён")
    return 0


# --------------------------------------------------------------- чтение

def cmd_whoami(ctx, a) -> int:
    bot = ctx.bot
    print(f"бот: @{bot['username']} (id {bot['id']}, {bot.get('first_name', '')})")
    owner = ctx.owner_or_none
    if owner is None:
        print("владелец не задан: запустите `stickerpack.py setup`")
        return 1
    try:
        chat = ctx.api.call("getChat", {"chat_id": owner})
        who = chat.get("username") or chat.get("first_name") or ""
        print(f"владелец {owner}: доступен ({who}) [{ctx.owner_source}]")
        return 0
    except ApiError as e:
        print(f"владелец {owner}: недоступен — {e.description}. "
              f"Владелец должен нажать «Старт» у @{bot['username']}")
        return 1


def private_senders(updates: list) -> list[dict]:
    """Разные не-боты из личных чатов: сначала тот, чьё последнее сообщение начинается с /start,
    дальше — по давности последнего сообщения (свежие раньше)."""
    last: dict[int, tuple[int, dict, dict, bool]] = {}
    last_start: int | None = None
    for i, u in enumerate(updates):
        msg = u.get("message") or {}
        frm = msg.get("from") or {}
        if (msg.get("chat") or {}).get("type") != "private" or not frm.get("id") or frm.get("is_bot"):
            continue
        is_start = str(msg.get("text") or "").lstrip().startswith("/start")
        last[frm["id"]] = (i, frm, msg, is_start)
        if is_start:
            last_start = frm["id"]
    people = []
    for uid, (i, frm, msg, is_start) in sorted(last.items(), key=lambda kv: kv[1][0], reverse=True):
        ts = msg.get("date")
        people.append({
            "id": uid,
            "name": " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x) or "—",
            "uname": f"@{frm['username']}" if frm.get("username") else "без username",
            "text": " ".join(str(msg.get("text") or "").split())[:30] or "(без текста)",
            "when": (time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts)) if isinstance(ts, (int, float))
                     else "время неизвестно"),
            "start": uid == last_start,
        })
    people.sort(key=lambda p: not p["start"])
    return people


def cmd_setup(ctx, a) -> int:
    """Первый запуск: проверить бота, определить и сохранить владельца."""
    bot = ctx.bot
    print(f"бот: @{bot['username']} (id {bot['id']}, {bot.get('first_name', '')})")
    owner = a.owner
    if owner is not None:
        print(f"владелец задан явно: {owner}")
    elif ctx.owner_or_none is not None:
        owner = ctx.owner_or_none
        print(f"владелец уже задан: {owner} [{ctx.owner_source}] "
              "(чтобы сменить — setup --owner ID)")
    else:
        try:
            updates = ctx.api.call("getUpdates", {"timeout": 0, "limit": 20})
        except ApiError as e:
            if e.code == 409:
                raise CliError(
                    f"бот @{bot['username']} занят другой программой: его сообщения уже принимает "
                    "агент или у него настроен webhook, поэтому getUpdates недоступен. Заведите для паков "
                    "отдельного бота в @BotFather или укажите владельца явно: "
                    "setup --owner ВАШ_ID (свой id покажет, например, @userinfobot)") from None
            raise
        people = private_senders(updates or [])
        if not people:
            raise CliError(f"боту @{bot['username']} пока никто не писал в личные сообщения. Откройте его в "
                           "Telegram, нажмите «Старт» и повторите setup")
        if len(people) > 1:
            lines = [f"боту @{bot['username']} писали разные люди — владельца автоматически не выбираю:"]
            for p in people:
                mark = "  ← последний /start" if p is people[0] and p["start"] else ""
                lines.append(f"  id {p['id']} · {p['name']} ({p['uname']}) · {p['when']} · «{p['text']}»{mark}")
            lines.append("Выберите свой id и повторите: setup --owner ВАШ_ID")
            raise CliError("\n".join(lines))
        user = people[0]
        owner = user["id"]
        print(f"боту писал один человек: {user['name']} ({user['uname']}), id {owner}")
        print("  если это не вы — повторите setup с --owner ВАШ_ID")
    try:
        chat = ctx.api.call("getChat", {"chat_id": owner})
    except ApiError as e:
        raise CliError(f"владелец {owner} недоступен боту: {e.description}. Владелец должен нажать «Старт» "
                       f"у @{bot['username']}") from None
    print(f"владелец {owner}: доступен ({chat.get('username') or chat.get('first_name') or ''})")
    config = dict(ctx.config)
    config["owner_id"] = owner
    if a.token_env_file:
        path = Path(a.token_env_file).expanduser().resolve()
        _, var = read_token_file(path, a.token_var)
        config["token_env_file"] = str(path)
        if var:
            config["token_var"] = var
        else:
            config.pop("token_var", None)
    if a.preview_root:
        config["preview_root"] = str(Path(a.preview_root).expanduser().resolve())
    if a.preview_url_base:
        config["preview_url_base"] = a.preview_url_base
    path = save_config(ctx.base, config)
    print(f"настройки сохранены: {path} (токен в файл не записывается)")
    print("дальше:\n"
          "  stickerpack.py info ИМЯ_НАБОРА                                  — посмотреть пак\n"
          "  stickerpack.py create мой_пак --title \"Название\" --kind emoji --from-dir ./готовое --dry-run")
    return 0


def cmd_info(ctx, a) -> int:
    name, _ = parse_set_ref(a.set)
    remote = ctx.get_set(name)
    stickers = remote["stickers"]
    fmts: dict[str, int] = defaultdict(int)
    for s in stickers:
        fmts[remote_format(s)] += 1
    repaint = any(s.get("needs_repainting") for s in stickers)
    print(f"{remote['name']} — «{remote['title']}»")
    print(f"тип: {remote.get('sticker_type', 'regular')}; штук: {len(stickers)}; форматы: "
          + ", ".join(f"{k} {v}" for k, v in fmts.items()) + f"; needs_repainting: {'да' if repaint else 'нет'}")
    for i, s in enumerate(stickers, 1):
        print(f"{i:>4}  {s.get('emoji', ''):<3} {remote_format(s):<8} {s.get('custom_emoji_id', '')}")
    return 0


def cmd_fetch(ctx, a) -> int:
    name, pick = parse_set_ref(a.set)
    remote = ctx.get_set(name)
    stickers = remote["stickers"]
    positions = parse_positions(pick, len(stickers), "выбор") if pick else list(range(1, len(stickers) + 1))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    items = []
    for pos in positions:
        st = stickers[pos - 1]
        data, ext = ctx.api.download(st["file_id"])
        fn = f"{pos:03d}{ext}"
        (out / fn).write_bytes(data)
        items.append({"pos": pos, "file": fn, "emoji": st.get("emoji"), "format": remote_format(st),
                      "custom_emoji_id": st.get("custom_emoji_id"), "file_unique_id": st["file_unique_id"],
                      "md5": hashlib.md5(data).hexdigest(), "needs_repainting": bool(st.get("needs_repainting"))})
        print(f"  {fn}  {st.get('emoji', '')}  {human_size(len(data))}")
    meta = {"set": remote["name"], "title": remote["title"], "type": remote.get("sticker_type", "regular"),
            "needs_repainting": any(i["needs_repainting"] for i in items), "items": items}
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    print(f"скачано {len(items)} из {len(stickers)} в {out}")
    return 0


def collect_file_ids(inputs: list[str]) -> list[str]:
    ids: list[str] = []

    def add(token: str):
        token = token.strip()
        stem = Path(token).stem if "." in Path(token).name else token
        if FILE_ID_RE.match(stem) and stem not in ids:
            ids.append(stem)

    for raw in inputs:
        p = Path(raw)
        if p.is_file() and zipfile.is_zipfile(p):
            with zipfile.ZipFile(p) as z:
                for entry in z.namelist():
                    parts = [x for x in entry.split("/") if x and x != "__MACOSX"]
                    if parts and not parts[0].startswith("."):
                        add(parts[0] if len(parts) > 1 else parts[0])
        elif p.is_dir():
            for child in sorted(p.iterdir(), key=natural_key):
                add(child.name)
        elif p.is_file():
            add(p.name)
        else:
            add(raw)
    return ids


def cmd_identify(ctx, a) -> int:
    ids = collect_file_ids(a.inputs)
    if not ids:
        raise CliError("не найдено ни одного file_id (архив/каталог/имя файла/строка)")
    decoded: dict[str, int] = {}
    for fid in ids:
        try:
            decoded[fid] = decode_file_id(fid)["id"]
        except CliError as e:
            print(f"  ⚠ {e}", file=sys.stderr)
    uniq = list(dict.fromkeys(str(v) for v in decoded.values()))
    found: dict[str, dict] = {}
    for i in range(0, len(uniq), 200):
        for st in ctx.api.call("getCustomEmojiStickers", {"custom_emoji_ids": uniq[i:i + 200]}):
            found[st["custom_emoji_id"]] = st
    sets: dict[str, list[dict]] = {}
    positions: dict[str, list[int]] = defaultdict(list)
    print(f"{'#':>3}  {'emoji':<4} {'custom_emoji_id':<20} пак / позиция")
    for n, fid in enumerate(ids, 1):
        if fid not in decoded:
            print(f"{n:>3}  —    не разобран")
            continue
        cid = str(decoded[fid])
        st = found.get(cid)
        if not st or not st.get("set_name"):
            print(f"{n:>3}  —    {cid:<20} не найден среди custom emoji (обычный стикер или удалён)")
            continue
        sname = st["set_name"]
        if sname not in sets:
            sets[sname] = ctx.api.get_set(sname)["stickers"]
        pos = next((i for i, s in enumerate(sets[sname], 1) if s.get("custom_emoji_id") == cid), None)
        if pos:
            positions[sname].append(pos)
        print(f"{n:>3}  {st.get('emoji', ''):<4} {cid:<20} {sname} #{pos or '?'}")
    print()
    for sname, pos in positions.items():
        print(f"{sname}: {len(pos)} из {len(ids)}, позиции в паке {compress_ranges(pos)} (всего в паке {len(sets[sname])})")
    return 0


# --------------------------------------------------------- превью: sheet / preview

SHEET_MARGIN = 16
TILE_W, TILE_H = 200, 252
TILE_IMG = 180
LABEL_FONT = 46
EMPTY_RATIO = 0.01
FRAME_FRACTIONS = (0, 0.5, 0.25, 0.75)
STALE_PREVIEW_RE = re.compile(r"^\d+\.(webm|webp|png|tgs)$|^sheet(_\d+)?\.(png|jpe?g)$")
FONT_CANDIDATES = ("DejaVuSans-Bold.ttf", "Arial Bold.ttf", "arialbd.ttf", "Helvetica.ttc",
                   "LiberationSans-Bold.ttf")


def sheet_size(n: int, cols: int) -> tuple[int, int]:
    rows = -(-n // cols)
    return 2 * SHEET_MARGIN + cols * TILE_W, 2 * SHEET_MARGIN + rows * TILE_H


def tile_origin(i: int, cols: int) -> tuple[int, int]:
    return SHEET_MARGIN + (i % cols) * TILE_W, SHEET_MARGIN + (i // cols) * TILE_H


def _font(size: int):
    from PIL import ImageFont
    for name in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def content_pixels(im) -> int:
    mask = im.getchannel("A").point(lambda v: 255 if v > ALPHA_TRIM_MAX else 0)
    return mask.histogram()[255]


def pick_frame(get):
    """Первый «непустой» кадр: начало, затем середина, четверти. get(доля 0..1) → RGBA или None."""
    best, best_px = None, -1
    for frac in FRAME_FRACTIONS:
        im = get(frac)
        if im is None:
            continue
        px = content_pixels(im)
        if px >= im.width * im.height * EMPTY_RATIO:
            return im
        if px > best_px:
            best, best_px = im, px
    if best is None:
        raise CliError("не удалось получить кадр")
    return best


def _video_duration(path: Path) -> float:
    try:
        return float(probe(path).get("format", {}).get("duration"))
    except (TypeError, ValueError):
        return 1.0


def _ffmpeg_frame(path: Path, t: float, out: Path, vp9_decoder: bool):
    cmd = ["ffmpeg", "-v", "error", "-y"]
    if vp9_decoder:
        cmd += ["-c:v", "libvpx-vp9"]
    if t > 0:
        cmd += ["-ss", f"{t:.3f}"]
    cmd += ["-i", str(path), "-frames:v", "1", "-pix_fmt", "rgba", str(out)]
    try:
        r = subprocess.run(cmd, capture_output=True)
    except FileNotFoundError:
        raise MissingToolError(f"ffmpeg не найден — {FFMPEG_HINT}") from None
    return r.returncode == 0 and out.is_file() and out.stat().st_size > 0


def video_frame_getter(path: Path, tmp: Path):
    """Кадры webm берутся декодером libvpx-vp9: встроенный декодер ffmpeg теряет альфа-канал."""
    Image, _ = pil()
    duration = _video_duration(path)
    is_webm = path.suffix.lower() == ".webm"

    def get(frac: float):
        out = tmp / f"frame_{frac}.png"
        t = max(0.0, min(duration * frac, duration - 0.05))
        ok = _ffmpeg_frame(path, t, out, is_webm) or (is_webm and _ffmpeg_frame(path, t, out, False))
        if not ok:
            return None
        with Image.open(out) as im:
            return im.convert("RGBA")

    return get


def pillow_frame_getter(im):
    n = getattr(im, "n_frames", 1)

    def get(frac: float):
        im.seek(int((n - 1) * frac + 0.5))
        return im.convert("RGBA")

    return get


def tile_picture(path: Path):
    """RGBA-картинка для плитки; None для TGS (его не отрисовать без Lottie)."""
    Image, ImageOps = pil()
    ext = path.suffix.lower()
    if ext == ".tgs":
        return None
    if ext in STATIC_EXT | {".gif"}:
        with Image.open(path) as im:
            if getattr(im, "n_frames", 1) > 1:
                frame = pick_frame(pillow_frame_getter(im))
            else:
                frame = ImageOps.exif_transpose(im).convert("RGBA")
            return frame.copy()
    with tempfile.TemporaryDirectory() as td:
        return pick_frame(video_frame_getter(path, Path(td)))


def render_sheet(entries: list[tuple[int, Path]], cols: int, dark: bool = False):
    """Контактный лист: плитка на файл, крупный номер под картинкой. → (Image RGB, предупреждения)."""
    Image, _ = pil()
    from PIL import ImageDraw
    bg, tile_bg, ink, line = ((23, 33, 43), (31, 44, 58), (240, 245, 250), (70, 86, 102)) if dark else \
        ((255, 255, 255), (243, 245, 247), (17, 17, 17), (205, 211, 218))
    sheet = Image.new("RGB", sheet_size(len(entries), cols), bg)
    draw = ImageDraw.Draw(sheet)
    font = _font(LABEL_FONT)
    warnings: list[str] = []

    def centered(text, cx, cy, fnt):
        l, t, r, b = draw.textbbox((0, 0), text, font=fnt)
        draw.text((cx - (l + r) / 2, cy - (t + b) / 2), text, font=fnt, fill=ink)

    for i, (number, path) in enumerate(entries):
        x, y = tile_origin(i, cols)
        draw.rounded_rectangle((x + 4, y + 4, x + TILE_W - 5, y + TILE_H - 5), radius=14, fill=tile_bg,
                               outline=line)
        box_cx, box_cy = x + TILE_W // 2, y + 10 + TILE_IMG // 2
        try:
            pic = tile_picture(path)
        except MissingToolError:
            raise
        except Exception as e:  # noqa: BLE001
            warnings.append(f"{path.name}: не удалось построить превью ({redact(str(e))[:120]})")
            centered("?", box_cx, box_cy, _font(80))
        else:
            if pic is None:
                draw.rectangle((x + 24, y + 34, x + TILE_W - 25, y + 34 + 132), outline=line, width=3)
                centered("TGS", box_cx, box_cy, _font(44))
            else:
                scale = min(TILE_IMG / pic.width, TILE_IMG / pic.height)
                size = (max(1, round(pic.width * scale)), max(1, round(pic.height * scale)))
                pic = pic.resize(size, Image.LANCZOS)
                sheet.paste(pic, (box_cx - size[0] // 2, box_cy - size[1] // 2), pic)
        centered(str(number), x + TILE_W // 2, y + 10 + TILE_IMG + (TILE_H - TILE_IMG - 20) // 2, font)
    return sheet, warnings


def collect_sheet_files(inputs: list[str]) -> list[Path]:
    out: list[Path] = []
    for raw in inputs:
        p = Path(raw)
        if p.is_dir():
            out.extend(sorted((x for x in p.iterdir() if x.is_file() and x.suffix.lower() in ALL_EXT),
                              key=natural_key))
        elif p.is_file():
            if p.suffix.lower() not in ALL_EXT:
                raise CliError(f"{p.name}: формат {p.suffix or '(нет расширения)'} не поддерживается")
            out.append(p)
        else:
            raise CliError(f"не найдено: {p}")
    if not out:
        raise CliError("нет файлов для листа (png/jpg/webp/gif/webm/mp4/tgs…)")
    return out


def write_sheets(entries: list[tuple[int, Path]], out: Path, cols: int | None, dark: bool,
                 per_sheet: int) -> list[Path]:
    if out.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
        raise CliError("--out: файл .png или .jpg")
    if per_sheet < 1:
        raise CliError("--per-sheet: хотя бы 1")
    out.parent.mkdir(parents=True, exist_ok=True)
    written = []
    for page, start in enumerate(range(0, len(entries), per_sheet)):
        chunk = entries[start:start + per_sheet]
        target = out if page == 0 else out.with_name(f"{out.stem}_{page + 1}{out.suffix}")
        sheet, warnings = render_sheet(chunk, max(1, min(cols or 5, len(chunk))), dark)
        for w in warnings:
            print(f"  ⚠ {w}", file=sys.stderr)
        sheet.save(target)
        written.append(target)
    return written


def cmd_sheet(ctx, a) -> int:
    paths = collect_sheet_files(a.inputs)
    entries = list(enumerate(paths, 1))
    written = write_sheets(entries, Path(a.out), a.cols, a.dark, a.per_sheet)
    for n, p in entries:
        print(f"{n:>4}  {p.name}")
    print(f"\nлист: {', '.join(str(w) for w in written)}")
    print("отправьте эту картинку человеку: номера на ней — это номера файлов выше")
    return 0


PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
body{{margin:0;padding:16px;font:15px/1.4 system-ui,sans-serif;background:#fff;color:#111}}
body.dark{{background:#17212b;color:#e8eef4}}
h1{{font-size:20px;margin:0 0 4px}}p{{margin:0 0 12px;opacity:.7}}
button{{font:inherit;padding:8px 14px;border-radius:8px;border:1px solid #8886;background:transparent;color:inherit;margin-bottom:14px}}
.g{{display:grid;grid-template-columns:repeat(auto-fill,minmax({cell}px,1fr));gap:10px}}
figure{{margin:0;text-align:center}}
video,img{{width:{size}px;height:{size}px;object-fit:contain}}
.tgs{{width:{size}px;height:{size}px;display:flex;align-items:center;justify-content:center;margin:auto;border:1px dashed #8888;border-radius:8px;font-size:12px}}
figcaption{{font-size:20px;font-weight:700}}
</style></head><body>
<h1>{title}</h1><p>{count} шт. · порядок как в наборе, номера с 1</p>
<button onclick="document.body.classList.toggle('dark')">Светлый / тёмный фон</button>
<div class="g">{cells}</div></body></html>
"""


def claim_preview_dir(d: Path, adopt: bool = False) -> None:
    """Готовит каталог превью. Писать и чистить можно только каталог, созданный этим скриптом
    (маркер PREVIEW_MARKER); adopt=True — каталог внутри состояния скрипта, маркер ставится задним числом."""
    if d.is_symlink():
        raise CliError(f"{d} — символическая ссылка: превью в такой каталог не пишется, укажите обычный "
                       "пустой или новый каталог")
    if d.exists() and not d.is_dir():
        raise CliError(f"{d} — не каталог")
    marker = d / PREVIEW_MARKER
    if not d.exists():
        d.mkdir(parents=True)
    elif not (marker.is_file() and not marker.is_symlink()):
        if not adopt and any(d.iterdir()):
            raise CliError(f"каталог {d} не пуст и создан не этим скриптом (нет маркера {PREVIEW_MARKER}), "
                           "поэтому файлы в нём не трогаются: выберите пустой или новый каталог")
        if marker.is_symlink():
            marker.unlink()
    if not marker.is_file():
        marker.write_text("создано stickerpack.py: файлы NN.ext и sheet*.png здесь перезаписываются\n")
    for f in d.iterdir():
        if (f.is_symlink() or f.is_file()) and STALE_PREVIEW_RE.match(f.name):
            f.unlink()


def writable_target(p: Path) -> Path:
    """Перед записью убирает симлинк по этому пути (сама цель не трогается)."""
    if p.is_symlink():
        p.unlink()
    elif p.is_dir():
        raise CliError(f"{p} — каталог, ожидался файл превью")
    return p


def write_preview(out: Path, files: list[Path], title: str, kind: str,
                  adopt: bool = False) -> tuple[Path, list[Path], list[Path]]:
    """Копирует файлы как NN.ext, пишет index.html и sheet.png. → (index, медиа, листы)."""
    claim_preview_dir(out, adopt)
    width = max(2, len(str(len(files))))
    copied, cells = [], []
    for n, src in enumerate(files, 1):
        fn = f"{n:0{width}d}{src.suffix.lower()}"
        shutil.copyfile(src, writable_target(out / fn))
        copied.append(out / fn)
        if fn.endswith(".webm"):
            tag = f'<video src="{fn}" autoplay loop muted playsinline></video>'
        elif fn.endswith(".tgs"):
            tag = '<div class="tgs">TGS</div>'
        else:
            tag = f'<img src="{fn}" alt="">'
        cells.append(f"<figure>{tag}<figcaption>{n}</figcaption></figure>")
    size = 64 if kind == "emoji" else 128
    index = writable_target(out / "index.html")
    index.write_text(PAGE.format(title=html.escape(title), count=len(cells), cells="".join(cells),
                                 size=size, cell=size + 16))
    sheets = write_sheets(list(enumerate(copied, 1)), out / "sheet.png", None, False, 40)
    for f in [index, *copied, *sheets]:
        f.chmod(0o644)
    return index, copied, sheets


def publish_preview(out: Path, dest: Path, files: list[Path]) -> None:
    claim_preview_dir(dest)
    for f in files:
        shutil.copyfile(f, writable_target(dest / f.name))
        (dest / f.name).chmod(0o644)
    dest.chmod(0o755)


def ensure_local_sources(ctx, name: str, remote: dict, state: dict) -> bool:
    """Скачивает через getFile файлы тех элементов, у которых нет исходника на диске."""
    files_dir = ctx.pack_dir(name) / "files"
    changed = False
    for i, it in enumerate(state["items"]):
        if it.get("src") and Path(it["src"]).is_file():
            continue
        files_dir.mkdir(parents=True, exist_ok=True)
        data, ext = ctx.api.download(remote["stickers"][i]["file_id"])
        path = files_dir / f"{item_key(it)}{ext}"
        path.write_bytes(data)
        it["src"], it["src_md5"] = str(path), hashlib.md5(data).hexdigest()
        changed = True
    return changed


def cmd_preview(ctx, a) -> int:
    name = ctx.full_name(a.short_name)
    publish = a.publish
    root = None
    if publish is not None:
        root = a.publish_root or ctx.config.get("preview_root")
        if not root:
            raise CliError("публикация не настроена: укажите --publish-root ПУТЬ (и --url-base ССЫЛКА) "
                           "или сохраните их через `setup --preview-root ПУТЬ --preview-url-base ССЫЛКА`")
    remote, state = refresh(ctx, name)
    kind = remote_kind(remote)
    changed = ensure_local_sources(ctx, name, remote, state)
    pname = None
    if publish is not None:
        pname = publish or state.get("preview_name") or re.sub(r"_by_[A-Za-z0-9_]+$", "", name, flags=re.I)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", pname):
            raise CliError("--publish: имя только из латиницы, цифр, «-» и «_»")
        if state.get("preview_name") != pname:
            state["preview_name"] = pname
            changed = True
    if changed:
        save_state(ctx, name, state)
    out = Path(a.out) if a.out else ctx.pack_dir(name) / "preview"
    index, copied, sheets = write_preview(out, [Path(it["src"]) for it in state["items"]],
                                          remote["title"], kind, adopt=not a.out)
    print(f"превью: {len(copied)} шт., порядок как в наборе")
    print(f"картинка с номерами: {', '.join(str(s) for s in sheets)}")
    print("  → отправьте эту картинку человеку; страница для браузера: " + str(index))
    if pname:
        dest = Path(root).expanduser() / pname
        publish_preview(out, dest, [index, *copied, *sheets])
        base = a.url_base or ctx.config.get("preview_url_base")
        if base:
            print(f"опубликовано: {base.rstrip('/')}/{pname}/")
        else:
            print(f"опубликовано в {dest} (preview_url_base не задан — ссылки нет)")
    return 0


# ---------------------------------------------------------------------- CLI

class SourceAction(argparse.Action):
    def __call__(self, parser, ns, values, option_string=None):
        lst = list(getattr(ns, "sources", None) or [])
        if self.dest == "files":
            lst.append(("files", values))
        elif self.dest == "from_dir":
            lst.append(("dir", values))
        else:
            lst.append(("set", values))
        ns.sources = lst


HELP_EPILOG = (
    "Откуда берутся настройки:\n"
    "  токен бота   --token-file [--token-var] → переменная STICKER_BOT_TOKEN → config.json\n"
    "               (token_env_file + token_var; сам токен в config.json не хранится)\n"
    "  владелец     --owner → STICKER_OWNER_ID → config.json (owner_id) → `setup`\n"
    "  каталог      STICKERPACKS_HOME → $HERMES_HOME/sticker-packs → ~/.sticker-packs\n"
    "               (там config.json и состояние паков: <пак>/pack.json, files/, preview/)\n"
    "Первый запуск: создайте бота в @BotFather, нажмите у него «Старт», задайте STICKER_BOT_TOKEN\n"
    "и выполните `stickerpack.py setup`. Позиции в командах — с 1.")


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--token-file", default=argparse.SUPPRESS, metavar="ПУТЬ",
                        help="файл с токеном бота: dotenv или просто токен (иначе STICKER_BOT_TOKEN)")
    common.add_argument("--token-var", default=argparse.SUPPRESS, metavar="ИМЯ",
                        help="имя переменной токена в dotenv (по умолчанию STICKER_BOT_TOKEN, затем "
                             "TELEGRAM_BOT_TOKEN)")
    common.add_argument("--owner", type=int, default=argparse.SUPPRESS, metavar="ID",
                        help="Telegram id владельца наборов (иначе STICKER_OWNER_ID, config.json, setup)")
    p = argparse.ArgumentParser(
        prog="stickerpack.py", parents=[common], epilog=HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Эмодзи-паки и стикерпаки Telegram через вашего бота: скачать, подготовить, создать, "
                    "изменить, показать превью с номерами. Токен никогда не печатается.")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="команда")

    def add(name, help_, func, epilog=None):
        sp = sub.add_parser(name, parents=[common], help=help_, description=help_, epilog=epilog,
                            formatter_class=argparse.RawDescriptionHelpFormatter)
        sp.set_defaults(func=func)
        return sp

    sp = add("setup", "первый запуск: проверить бота, найти и сохранить владельца наборов", cmd_setup,
             "Без --owner смотрит, кто писал боту в личные сообщения («Старт»). Один человек — он и владелец;\n"
             "несколько разных — владельца сам не выбирает, показывает список (последний /start первым):\n"
             "повторите с --owner ID. getUpdates без подтверждения, ничего не теряется.\n"
             "Если бота уже опрашивает другая программа\n"
             "(ответ 409), используйте отдельного бота или --owner. Токен в config.json не пишется.")
    sp.add_argument("--token-env-file", metavar="ПУТЬ",
                    help="запомнить путь к dotenv с токеном (имя переменной — --token-var)")
    sp.add_argument("--preview-root", metavar="ПУТЬ",
                    help="запомнить каталог веб-сервера для `preview --publish`")
    sp.add_argument("--preview-url-base", metavar="ССЫЛКА",
                    help="запомнить адрес этого каталога, например https://example.com/previews/")
    add("whoami", "бот (username, id) и доступность владельца", cmd_whoami)
    sp = add("info", "название, тип, форматы и список элементов набора", cmd_info)
    sp.add_argument("set", help="имя набора или ссылка t.me/addemoji/…")
    sp = add("identify", "узнать исходный пак по file_id (архив-выгрузка, каталог, имена файлов, строки)", cmd_identify,
             "Имена файлов/каталогов в выгрузках — Bot API file_id. Для custom emoji id документа = custom_emoji_id,\n"
             "по нему находится исходный набор и позиция в нём.")
    sp.add_argument("inputs", nargs="+", help="zip, каталог, файл или file_id")
    sp = add("fetch", "скачать файлы набора (NNN.ext) и meta.json", cmd_fetch,
             "Пример: fetch AIByVolodya --out /tmp/pack   или   fetch AIByVolodya:1,5-9 --out /tmp/pack")
    sp.add_argument("set", help="имя набора, можно с выбором позиций: имя:1,3,5-9")
    sp.add_argument("--out", required=True, help="каталог для файлов")
    sp = add("prepare", "привести файлы к требованиям Telegram (WEBP/WEBM)", cmd_prepare,
             "emoji — ровно 100×100; sticker — одна сторона 512. Статичные картинки сначала обрезаются по альфе\n"
             "(alpha ≤ 8 — прозрачно), затем вписываются с отступом --pad; --no-trim отключает обрезку. Анимация → WEBM VP9 без звука, ≤3 с, ≤30 к/с, ≤256 КБ.\n"
             ".tgs копируется как есть после проверки (gzip, ≤64 КБ).")
    sp.add_argument("files", nargs="+")
    sp.add_argument("--kind", required=True, choices=["emoji", "sticker"])
    sp.add_argument("--out", required=True)
    sp.add_argument("--pad", type=int, default=None, metavar="ПИКС",
                    help="отступ вокруг рисунка в пикселях целевого размера (по умолчанию 2 для emoji, "
                         "8 для sticker; с --no-trim — 0)")
    sp.add_argument("--no-trim", action="store_true",
                    help="не обрезать прозрачные поля (вписывать весь холст, как раньше)")
    sp = add("validate", "проверить файлы по правилам Telegram; код выхода ≠0 при нарушениях", cmd_validate)
    sp.add_argument("files", nargs="+")
    sp.add_argument("--kind", required=True, choices=["emoji", "sticker"])

    def sources(sp, with_dir=True):
        sp.add_argument("--files", nargs="+", action=SourceAction, metavar="ФАЙЛ",
                        help="готовые файлы (порядок = порядок в паке)")
        if with_dir:
            sp.add_argument("--from-dir", action=SourceAction, metavar="КАТАЛОГ",
                            help="все файлы каталога по имени (с meta.json от fetch/prepare берутся emoji)")
        sp.add_argument("--from-set", action=SourceAction, metavar="НАБОР[:PICK]",
                        help="взять из набора, напр. AIByVolodya:1,3,5-9; можно повторять")
        sp.add_argument("--emoji", default="⭐", help="emoji для файлов без meta.json (по умолчанию ⭐; несколько — через запятую)")
        sp.set_defaults(sources=[])

    sp = add("create", "создать набор и добавить элементы (повторный запуск продолжает, не дублируя)", cmd_create,
             "Пример: create my_pack --title \"Мои\" --kind emoji --from-dir ./ready --dry-run")
    sp.add_argument("short_name", help="короткое имя; _by_<бот> добавится само")
    sp.add_argument("--title", required=True, help="название, 1–64 символа")
    sp.add_argument("--kind", required=True, choices=["emoji", "sticker"])
    sp.add_argument("--repaint", action="store_true", help="эмодзи перекрашиваются под цвет текста (needs_repainting)")
    sp.add_argument("--dry-run", action="store_true", help="только проверка файлов и свободного имени")
    sources(sp)
    sp = add("add", "добавить элементы в конец (и переставить с --at)", cmd_add)
    sp.add_argument("short_name")
    sp.add_argument("--at", type=int, help="позиция (с 1), с которой поставить добавленные подряд")
    sources(sp)
    sp = add("delete", "удалить элементы по текущим позициям", cmd_delete)
    sp.add_argument("short_name")
    sp.add_argument("--pos", required=True, help="позиции: 3,5-7")
    sp = add("order", "задать новый порядок текущими позициями", cmd_order,
             "Пример: --order \"21,5-20,22\". Не упомянутые — в конец в прежнем порядке; повторы — ошибка.")
    sp.add_argument("short_name")
    sp.add_argument("--order", required=True)
    sp = add("title", "сменить название", cmd_title)
    sp.add_argument("short_name")
    sp.add_argument("new_title", metavar="название")
    sp = add("cover", "обложка: для эмодзи — элемент по --pos, для стикеров — файл --file", cmd_cover)
    sp.add_argument("short_name")
    sp.add_argument("--pos", type=int)
    sp.add_argument("--file", help="только для стикерпаков: WEBP/PNG 100×100 ≤128 КБ, TGS ≤32 КБ, "
                                   "WEBM VP9 100×100 ≤32 КБ")
    sp = add("emoji", "сменить emoji (и ключевые слова) элемента", cmd_emoji)
    sp.add_argument("short_name")
    sp.add_argument("--pos", type=int, required=True)
    sp.add_argument("--emoji", required=True)
    sp.add_argument("--keywords", help="ключевые слова через запятую")
    sp = add("sheet", "контактный лист с крупными номерами по файлам/каталогам (PNG для чата)", cmd_sheet,
             "Для видео берётся первый непустой кадр (если первый почти прозрачный — из середины),\n"
             "для TGS — плашка «TGS». Нужен, чтобы показать человеку кандидатов до сборки пака.\n"
             "Много файлов делятся на листы (--per-sheet): sheet.png, sheet_2.png… с общей нумерацией.")
    sp.add_argument("inputs", nargs="+", metavar="файл_или_каталог")
    sp.add_argument("--out", required=True, metavar="ФАЙЛ", help="куда сохранить, например sheet.png")
    sp.add_argument("--cols", type=int, help="колонок в сетке (по умолчанию 5)")
    sp.add_argument("--dark", action="store_true", help="тёмный фон")
    sp.add_argument("--per-sheet", type=int, default=40, metavar="N", help="плиток на лист (по умолчанию 40)")
    sp = add("preview", "превью пака по текущему порядку: sheet.png с номерами и index.html", cmd_preview,
             "Файлы кладутся как 01.ext, 02.ext… рядом с sheet.png и index.html (светлый/тёмный фон,\n"
             "мобильная вёрстка). Старые файлы вида NN.ext и sheet*.png в каталоге заменяются.\n"
             "--publish [ИМЯ] дополнительно копирует страницу в <preview_root>/ИМЯ/ и печатает ссылку;\n"
             "preview_root и preview_url_base задаются через setup или --publish-root/--url-base.")
    sp.add_argument("short_name")
    sp.add_argument("--out", metavar="КАТАЛОГ", help="куда писать (по умолчанию <каталог>/<пак>/preview/)")
    sp.add_argument("--publish", nargs="?", const="", metavar="ИМЯ",
                    help="опубликовать на веб-сервере; ИМЯ запоминается (без него — сохранённое или имя пака)")
    sp.add_argument("--publish-root", metavar="ПУТЬ", help="каталог веб-сервера (иначе preview_root из config.json)")
    sp.add_argument("--url-base", metavar="ССЫЛКА", help="адрес каталога (иначе preview_url_base из config.json)")
    sp = add("delete-set", "удалить набор целиком (нужно подтверждение полным именем)", cmd_delete_set)
    sp.add_argument("short_name")
    sp.add_argument("--confirm", required=True, help="полное имя набора, как оно заканчивается на _by_<бот>")
    return p


NO_TOKEN = {"prepare", "validate", "sheet"}


def main(argv: list[str] | None = None, api=None, base: Path | None = None) -> int:
    if sys.version_info < (3, 10):
        print("ошибка: нужен Python 3.10 или новее", file=sys.stderr)
        return 1
    parser = build_parser()
    a = parser.parse_args(argv)
    for k in ("token_file", "token_var", "owner"):
        if not hasattr(a, k):
            setattr(a, k, None)
    try:
        home = Path(base) if base else home_dir()
        config = load_config(home)
        if api is None and a.cmd not in NO_TOKEN:
            token_file = a.token_file or (getattr(a, "token_env_file", None) if a.cmd == "setup" else None)
            api = Api(resolve_token(token_file, a.token_var, config))
        owner, source = resolve_owner(a.owner, config)
        ctx = Ctx(api, owner, home, config, source)
        return a.func(ctx, a)
    except CliError as e:
        print(f"ошибка: {redact(str(e))}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("прервано", file=sys.stderr)
        return 130
    except Exception as e:  # noqa: BLE001
        print(f"ошибка: {type(e).__name__}: {redact(str(e))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
