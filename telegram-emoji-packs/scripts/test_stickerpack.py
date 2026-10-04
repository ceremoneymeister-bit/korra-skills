"""Офлайн-тесты stickerpack.py: сеть не используется (API подменяется FakeApi)."""
import gzip
import hashlib
import io
import json
import os
import shutil
import random
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import stickerpack as sp  # noqa: E402

TOKEN = "1234567890:AAFakeTokenForTestsOnly_abcdefghijk"

VECTORS = {
    "CAACAgIAAxUAAWrA6c2sFcfyKMoKta-CSFTp_xn3AAJlnQACAuGASoqhjH6tKJAgPQQ": 5368538154531855717,
    "CAACAgIAAxUAAWrA6c1-OnO99dS8M2W2XZrsnKvMAAJLpgACCexISV7Bsgdqq-pgPQQ": 5280730086490809931,
    "CAACAgIAAxUAAWrA6c1KnmHRAmX-vttEwp9HGgKbAAIDpAAC6F5JSaWMBO-IcTGjPQQ": 5280856388594082819,
}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(sp.time, "sleep", lambda s: None)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for k in ("STICKER_BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "STICKER_OWNER_ID", "HERMES_HOME"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("STICKERPACKS_HOME", str(tmp_path / "home"))


# ------------------------------------------------------------------ разбор

@pytest.mark.parametrize("fid,expected", VECTORS.items())
def test_decode_file_id(fid, expected):
    assert sp.decode_file_id(fid)["id"] == expected


def test_decode_file_id_garbage():
    with pytest.raises(sp.CliError):
        sp.decode_file_id("AAAA")


def test_parse_positions():
    assert sp.parse_positions("1,3,5-9", 10) == [1, 3, 5, 6, 7, 8, 9]
    assert sp.parse_positions("9-7, 1", 10) == [9, 8, 7, 1]
    assert sp.parse_positions("10", 10) == [10]


@pytest.mark.parametrize("spec", ["0", "11", "1,1", "1-3,2", "a", "1-", "", "3-12"])
def test_parse_positions_errors(spec):
    with pytest.raises(sp.CliError):
        sp.parse_positions(spec, 10)


def test_resolve_order_appends_rest_in_old_order():
    full, rest = sp.resolve_order("4,2-3", 6)
    assert full == [4, 2, 3, 1, 5, 6] and rest == [1, 5, 6]
    with pytest.raises(sp.CliError):
        sp.resolve_order("2,2", 5)
    with pytest.raises(sp.CliError):
        sp.resolve_order("1,9", 5)


def test_parse_set_ref():
    assert sp.parse_set_ref("AIByVolodya:1,3-5") == ("AIByVolodya", "1,3-5")
    assert sp.parse_set_ref("https://t.me/addemoji/AIByVolodya") == ("AIByVolodya", None)


def test_compress_ranges():
    assert sp.compress_ranges([1, 2, 3, 5, 7, 8]) == "1-3,5,7-8"


def test_name_suffix_and_validation():
    assert sp.normalize_name("mypack", "KorraBot") == "mypack_by_KorraBot"
    assert sp.normalize_name("mypack_BY_korrabot", "KorraBot") == "mypack_BY_korrabot"
    sp.validate_name("mypack_by_korrabot")
    for bad in ["1pack_by_b", "pa-ck_by_b", "pa__ck_by_b", "x" * 70 + "_by_b", "пак_by_b"]:
        with pytest.raises(sp.CliError):
            sp.validate_name(bad)
    sp.validate_title("a")
    for bad in ["", "x" * 65]:
        with pytest.raises(sp.CliError):
            sp.validate_title(bad)


def test_parse_env_quotes_and_export(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# c\nOTHER=1\nexport TELEGRAM_BOT_TOKEN=\"abc:def\"\n")
    assert sp.parse_env(env.read_text())["TELEGRAM_BOT_TOKEN"] == "abc:def"
    assert sp.resolve_token(token_file=str(env), env={}) == "abc:def"
    raw = tmp_path / "raw"
    raw.write_text("999:rawtoken\n")
    assert sp.resolve_token(token_file=str(raw), env={}) == "999:rawtoken"


# --------------------------------------------------------------- перестановка

def apply_moves(cur, moves):
    cur = list(cur)
    for x, pos in moves:
        cur.remove(x)
        cur.insert(pos, x)
    return cur


def lis_len(seq):
    best = []
    for i, x in enumerate(seq):
        best.append(1 + max([best[j] for j in range(i) if seq[j] < x], default=0))
    return max(best, default=0)


def test_plan_moves_random_reaches_target_minimally():
    rnd = random.Random(7)
    for _ in range(300):
        n = rnd.randint(1, 14)
        cur = list(range(n))
        target = cur[:]
        rnd.shuffle(target)
        moves = sp.plan_moves(cur, target)
        assert apply_moves(cur, moves) == target
        idx = {x: i for i, x in enumerate(cur)}
        assert len(moves) == n - lis_len([idx[x] for x in target])


def test_plan_moves_noop_and_single():
    assert sp.plan_moves([0, 1, 2], [0, 1, 2]) == []
    assert len(sp.plan_moves([0, 1, 2, 3], [3, 0, 1, 2])) == 1


# --------------------------------------------------------- prepare / validate

def run_prepare(tmp_path, files, kind, *extra):
    out = tmp_path / "out"
    rc = sp.main(["prepare", *map(str, files), "--kind", kind, "--out", str(out), *extra])
    return rc, out


def test_prepare_png_to_emoji_and_sticker(tmp_path):
    from PIL import Image
    src = tmp_path / "pic.png"
    Image.new("RGBA", (300, 200), (255, 0, 0, 255)).save(src)
    rc, out = run_prepare(tmp_path, [src], "emoji")
    assert rc == 0
    res = out / "pic.webp"
    with Image.open(res) as im:
        assert im.size == (100, 100)
        assert im.getpixel((50, 2))[3] == 0  # прозрачные поля сверху
        assert im.getpixel((50, 50))[3] == 255
    assert sp.validate_file(res, "emoji") == []
    assert sp.validate_file(src, "emoji")  # исходник не подходит
    rc, out2 = run_prepare(tmp_path, [src], "sticker")
    with Image.open(out2 / "pic.webp") as im:
        assert im.size == (512, 347)  # 496×331 + отступ 8 с каждой стороны
    assert sp.validate_file(out2 / "pic.webp", "sticker") == []
    rc, out3 = run_prepare(tmp_path, [src], "sticker", "--no-trim")
    with Image.open(out3 / "pic.webp") as im:
        assert im.size == (512, 341)


def make_padded_png(path, size=(400, 300), box=(100, 50), shadow=False):
    from PIL import Image
    im = Image.new("RGBA", size, (0, 0, 0, 0))
    cx, cy = size[0] // 2, size[1] // 2
    left, top = cx - box[0] // 2, cy - box[1] // 2
    im.paste(Image.new("RGBA", box, (255, 0, 0, 255)), (left, top))
    if shadow:
        im.putpixel((5, 5), (0, 0, 0, 8))
    im.save(path)


def content_bbox(path):
    from PIL import Image
    with Image.open(path) as im:
        return im.convert("RGBA").getchannel("A").point(lambda v: 255 if v > 8 else 0).getbbox()


def test_prepare_trims_transparent_margins(tmp_path, capsys):
    src = tmp_path / "pic.png"
    make_padded_png(src, shadow=True)
    rc, out = run_prepare(tmp_path, [src], "emoji")
    assert rc == 0
    left, top, right, bottom = content_bbox(out / "pic.webp")
    assert abs((right - left) - (100 - 2 * 2)) <= 1
    text = capsys.readouterr().out
    assert "400×300" in text and "100×50" in text and "100×100" in text
    rc, out = run_prepare(tmp_path, [src], "emoji", "--pad", "10")
    left, _, right, _ = content_bbox(out / "pic.webp")
    assert abs((right - left) - 80) <= 1


def test_prepare_no_trim_keeps_old_result(tmp_path):
    src = tmp_path / "pic.png"
    make_padded_png(src)
    rc, out = run_prepare(tmp_path, [src], "emoji", "--no-trim")
    assert rc == 0
    left, _, right, _ = content_bbox(out / "pic.webp")
    assert abs((right - left) - 25) <= 1  # 100 из 400 пикселей холста → 25 из 100


def test_prepare_sticker_trim_pad(tmp_path):
    from PIL import Image
    src = tmp_path / "pic.png"
    make_padded_png(src)
    rc, out = run_prepare(tmp_path, [src], "sticker")
    assert rc == 0
    res = out / "pic.webp"
    with Image.open(res) as im:
        assert max(im.size) == 512
    left, _, right, _ = content_bbox(res)
    assert abs((right - left) - (512 - 16)) <= 1
    assert sp.validate_file(res, "sticker") == []


def test_prepare_fully_transparent_is_error(tmp_path, capsys):
    from PIL import Image
    src = tmp_path / "empty.png"
    Image.new("RGBA", (200, 200), (255, 255, 255, 0)).save(src)
    rc, out = run_prepare(tmp_path, [src], "emoji")
    assert rc == 1
    assert "полностью прозрачная" in capsys.readouterr().out
    assert not (out / "empty.webp").exists() or sp.validate_file(out / "empty.webp", "emoji")


def test_prepare_bad_pad_is_error(tmp_path, capsys):
    src = tmp_path / "pic.png"
    make_padded_png(src)
    rc, _ = run_prepare(tmp_path, [src], "emoji", "--pad", "60")
    assert rc == 1
    assert "--pad" in capsys.readouterr().out


def test_prepare_animated_ignores_trim_options(tmp_path):
    src = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=25",
                    "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)], check=True)
    for extra in (["--no-trim"], ["--pad", "5"]):
        rc, out = run_prepare(tmp_path, [src], "emoji", *extra)
        assert rc == 0
        facts = sp.video_facts(out / "clip.webm")
        assert (facts["width"], facts["height"]) == (100, 100)


def test_prepare_video_with_audio(tmp_path):
    src = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25",
                    "-f", "lavfi", "-i", "sine=frequency=440", "-t", "5", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", str(src)], check=True)
    rc, out = run_prepare(tmp_path, [src], "emoji")
    assert rc == 0
    res = out / "clip.webm"
    facts = sp.video_facts(res)
    assert facts["streams"] == ["video"]  # звука нет
    assert facts["duration"] <= 3.0
    assert (facts["width"], facts["height"]) == (100, 100)
    assert facts["codec"] == "vp9"
    assert res.stat().st_size <= 256 * 1024
    assert sp.validate_file(res, "emoji") == []
    assert sp.validate_file(res, "sticker")  # для стикера 100×100 мало


def test_validate_reports_violations(tmp_path, capsys):
    src = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=128x128:rate=25",
                    "-f", "lavfi", "-i", "sine", "-t", "4", "-c:v", "libx264", "-c:a", "aac", str(src)], check=True)
    webm = tmp_path / "long.webm"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-c:v", "libvpx-vp9", "-c:a", "libopus",
                    str(webm)], check=True)
    bad = sp.validate_file(webm, "emoji")
    text = " ".join(bad)
    assert "100×100" in text and "длительность" in text and "звука" in text
    assert sp.main(["validate", str(webm), "--kind", "emoji"]) == 1


def test_tgs_checked_not_converted(tmp_path):
    import gzip
    good = tmp_path / "ok.tgs"
    good.write_bytes(gzip.compress(b'{"v":"5"}'))
    bad = tmp_path / "bad.tgs"
    bad.write_bytes(b"not gzip")
    big = tmp_path / "big.tgs"
    big.write_bytes(gzip.compress(random.Random(1).randbytes(80000)))
    assert sp.validate_file(good, "emoji") == []
    assert sp.validate_file(bad, "emoji")
    assert sp.validate_file(big, "emoji")
    rc, out = run_prepare(tmp_path, [good], "emoji")
    assert rc == 0 and (out / "ok.tgs").read_bytes() == good.read_bytes()


# ------------------------------------------------------------ токен в ошибках

def make_api():
    return sp.Api(TOKEN, pause=0)


def test_http_error_hides_token(monkeypatch):
    body = json.dumps({"ok": False, "error_code": 400,
                       "description": f"Bad Request: плохо {TOKEN}"}).encode()

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 400, "Bad", {}, io.BytesIO(body))

    monkeypatch.setattr(sp.urllib.request, "urlopen", boom)
    with pytest.raises(sp.ApiError) as e:
        make_api().call("getMe")
    assert TOKEN not in str(e.value) and "getMe" in str(e.value) and "Bad Request" in str(e.value)


def test_network_error_hides_token(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.URLError(f"failed {req.full_url}")

    monkeypatch.setattr(sp.urllib.request, "urlopen", boom)
    with pytest.raises(sp.ApiError) as e:
        make_api().call("getMe")
    assert TOKEN not in str(e.value) and "api.telegram.org/bot" not in str(e.value).replace("bot<токен скрыт>", "")


def test_main_prints_no_token(monkeypatch, capsys):
    def boom(req, timeout=None):
        raise urllib.error.URLError(req.full_url)

    monkeypatch.setattr(sp.urllib.request, "urlopen", boom)
    assert sp.main(["whoami"], api=make_api()) == 1
    err = capsys.readouterr()
    assert TOKEN not in err.out + err.err and "ошибка" in err.err


def test_redact_generic_pattern():
    assert "AAFake" not in sp.redact("https://api.telegram.org/bot999999:AAFakeFakeFakeFakeFakeFake/getMe")


def test_retry_after_waits_and_repeats(monkeypatch):
    calls = []
    sleeps = []
    monkeypatch.setattr(sp.time, "sleep", sleeps.append)

    class Resp:
        def __init__(self, data):
            self.data = data

        def read(self):
            return json.dumps(self.data).encode()

    answers = [{"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 3}},
               {"ok": True, "result": {"id": 1}}]

    def fake(req, timeout=None):
        calls.append(1)
        return Resp(answers.pop(0))

    monkeypatch.setattr(sp.urllib.request, "urlopen", fake)
    assert make_api().call("getMe") == {"id": 1}
    assert len(calls) == 2 and 4 in sleeps


# ----------------------------------------------------- сценарии на FakeApi

class FakeApi(sp.Api):
    def __init__(self, fail_after_uploads=None):
        super().__init__(TOKEN, pause=0)
        self.sets = {}
        self.uploads = {}
        self.log = []
        self.fail_after_uploads = fail_after_uploads
        self.next_id = 0
        self.updates = []
        self.updates_error = None
        self.chat_error = None

    def _sticker(self, upload, emoji, fmt):
        h = int(hashlib.md5(upload["data"]).hexdigest()[:15], 16)
        return {"file_id": f"fid{h}", "file_unique_id": f"u{h}", "custom_emoji_id": str(h),
                "emoji": emoji[0], "is_video": fmt == "video", "is_animated": fmt == "animated"}

    def call(self, method, params=None, files=None):
        params = params or {}
        self.log.append(method)
        if method == "getMe":
            return {"id": 1, "username": "testbot", "first_name": "T"}
        if method == "getChat":
            if self.chat_error:
                raise sp.ApiError(method, self.chat_error, 400)
            return {"id": params["chat_id"], "username": "owner"}
        if method == "getUpdates":
            if self.updates_error:
                raise self.updates_error
            assert params == {"timeout": 0, "limit": 20}  # без offset: ничего не подтверждаем
            return self.updates
        if method == "uploadStickerFile":
            if self.fail_after_uploads is not None and self.log.count("uploadStickerFile") > self.fail_after_uploads:
                raise sp.ApiError(method, "Internal Server Error")
            self.next_id += 1
            fid = f"up{self.next_id}"
            self.uploads[fid] = {"data": Path(files["sticker"]).read_bytes(), "fmt": params["sticker_format"]}
            return {"file_id": fid}
        if method == "getStickerSet":
            s = self.sets.get(params["name"])
            if not s:
                raise sp.ApiError(method, "Bad Request: STICKERSET_INVALID")
            return json.loads(json.dumps({"name": params["name"], "title": s["title"],
                                          "sticker_type": s["type"], "stickers": s["stickers"]}))
        if method in ("createNewStickerSet", "addStickerToSet"):
            inp = params["stickers"][0] if method == "createNewStickerSet" else params["sticker"]
            st = self._sticker(self.uploads[inp["sticker"]], inp["emoji_list"], inp["format"])
            if method == "createNewStickerSet":
                assert params["name"] not in self.sets
                self.sets[params["name"]] = {"title": params["title"], "type": params["sticker_type"],
                                             "stickers": [st], "params": params}
            else:
                self.sets[params["name"]]["stickers"].append(st)
            return True
        owner = lambda fid: next((s for s in self.sets.values() for x in s["stickers"] if x["file_id"] == fid), None)  # noqa: E731
        if method == "deleteStickerFromSet":
            s = owner(params["sticker"])
            s["stickers"] = [x for x in s["stickers"] if x["file_id"] != params["sticker"]]
            return True
        if method == "setStickerPositionInSet":
            s = owner(params["sticker"])
            x = next(x for x in s["stickers"] if x["file_id"] == params["sticker"])
            s["stickers"].remove(x)
            s["stickers"].insert(params["position"], x)
            return True
        if method == "setStickerSetTitle":
            self.sets[params["name"]]["title"] = params["title"]
            return True
        if method == "setStickerEmojiList":
            next(x for s in self.sets.values() for x in s["stickers"] if x["file_id"] == params["sticker"])["emoji"] = params["emoji_list"][0]
            return True
        if method in ("setCustomEmojiStickerSetThumbnail", "setStickerKeywords", "setStickerSetThumbnail"):
            return True
        if method == "deleteStickerSet":
            del self.sets[params["name"]]
            return True
        raise AssertionError(f"неожиданный метод {method}")

    def download(self, file_id):
        for s in self.sets.values():
            for x in s["stickers"]:
                if x["file_id"] == file_id:
                    return b"WEBM" + file_id.encode(), ".webm"
        raise AssertionError("нет такого файла")


@pytest.fixture
def work(tmp_path):
    from PIL import Image
    files = []
    for i in range(1, 7):
        p = tmp_path / "src" / f"{i:02d}.webp"
        p.parent.mkdir(exist_ok=True)
        img = Image.new("RGBA", (100, 100), (i * 30, 10, 10, 255))
        img.save(p, "WEBP")
        files.append(p)
    return tmp_path, files


def run(tmp, api, *argv):
    return sp.main(["--owner", "42", *argv], api=api, base=tmp / "state")


def names(api, name="demo_by_testbot"):
    return [s["emoji"] for s in api.sets[name]["stickers"]]


def pack_state(tmp, name="demo_by_testbot"):
    return json.loads((tmp / "state" / name / "pack.json").read_text())


def test_create_dry_run_does_not_touch(work):
    tmp, files = work
    api = FakeApi()
    assert run(tmp, api, "create", "demo", "--title", "T", "--kind", "emoji", "--dry-run",
               "--files", *map(str, files)) == 0
    assert api.sets == {} and not (tmp / "state").exists()
    assert "uploadStickerFile" not in api.log


def test_create_rejects_bad_files(work, capsys):
    tmp, files = work
    from PIL import Image
    bad = tmp / "big.png"
    Image.new("RGBA", (300, 200)).save(bad)
    assert run(tmp, FakeApi(), "create", "demo", "--title", "T", "--kind", "emoji", "--files", str(bad)) == 1
    assert "prepare" in capsys.readouterr().err


def test_create_resumes_without_duplicates(work):
    tmp, files = work
    api = FakeApi(fail_after_uploads=3)
    argv = ["create", "demo", "--title", "Демо", "--kind", "emoji", "--emoji", "😀", "--files", *map(str, files)]
    assert run(tmp, api, *argv) == 1
    assert len(api.sets["demo_by_testbot"]["stickers"]) == 3
    assert [i["src_md5"] for i in pack_state(tmp)["items"]] == [sp.md5_of(f) for f in files[:3]]
    api.fail_after_uploads = None
    assert run(tmp, api, *argv) == 0
    assert len(api.sets["demo_by_testbot"]["stickers"]) == 6
    assert run(tmp, api, *argv) == 0  # третий запуск ничего не добавляет
    assert len(api.sets["demo_by_testbot"]["stickers"]) == 6
    st = pack_state(tmp)
    assert st["kind"] == "emoji" and st["title"] == "Демо"
    assert [i["src_md5"] for i in st["items"]] == [sp.md5_of(f) for f in files]
    assert api.sets["demo_by_testbot"]["params"]["sticker_type"] == "custom_emoji"


def make_pack(work, api, n=6):
    tmp, files = work
    assert run(tmp, api, "create", "demo", "--title", "T", "--kind", "emoji", "--files", *map(str, files[:n])) == 0
    return tmp, files


def ids(api):
    return [s["custom_emoji_id"] for s in api.sets["demo_by_testbot"]["stickers"]]


def test_order_delete_title_emoji_cover(work):
    api = FakeApi()
    tmp, files = make_pack(work, api)
    before = ids(api)
    assert run(tmp, api, "order", "demo", "--order", "6,2-3") == 0
    assert ids(api) == [before[i - 1] for i in (6, 2, 3, 1, 4, 5)]
    assert [i["custom_emoji_id"] for i in pack_state(tmp)["items"]] == ids(api)
    assert run(tmp, api, "order", "demo", "--order", "1,1") == 1
    now = ids(api)
    assert run(tmp, api, "delete", "demo", "--pos", "2,4-5") == 0
    assert ids(api) == [now[0], now[2], now[5]]
    assert len(pack_state(tmp)["items"]) == 3
    assert run(tmp, api, "title", "demo", "Новое") == 0 and api.sets["demo_by_testbot"]["title"] == "Новое"
    assert run(tmp, api, "emoji", "demo", "--pos", "2", "--emoji", "😎", "--keywords", "a,b") == 0
    assert names(api)[1] == "😎"
    assert run(tmp, api, "cover", "demo", "--pos", "1") == 0
    assert "setCustomEmojiStickerSetThumbnail" in api.log
    assert run(tmp, api, "cover", "demo") == 1


def test_add_at_position(work):
    api = FakeApi()
    tmp, files = work
    assert run(tmp, api, "create", "demo", "--title", "T", "--kind", "emoji", "--files", *map(str, files[:4])) == 0
    before = ids(api)
    assert run(tmp, api, "add", "demo", "--files", str(files[4]), str(files[5]), "--at", "2") == 0
    after = ids(api)
    assert after[0] == before[0] and after[3:] == before[1:]
    assert len(after) == 6
    assert run(tmp, api, "add", "demo", "--files", str(files[4])) == 0  # повтор не дублирует
    assert len(ids(api)) == 6
    assert run(tmp, api, "add", "demo", "--files", str(files[0]), "--at", "99") == 1


def test_external_change_warns_and_takes_fact(work, capsys):
    api = FakeApi()
    tmp, files = make_pack(work, api, 4)
    api.sets["demo_by_testbot"]["stickers"].reverse()
    del api.sets["demo_by_testbot"]["stickers"][0]
    from PIL import Image
    p = tmp / "ext.webp"
    Image.new("RGBA", (100, 100), (1, 2, 3, 255)).save(p, "WEBP")
    up = api.call("uploadStickerFile", {"sticker_format": "static"}, files={"sticker": p})
    api.call("addStickerToSet", {"name": "demo_by_testbot",
                                 "sticker": {"sticker": up["file_id"], "format": "static", "emoji_list": ["🔥"]}})
    capsys.readouterr()
    assert run(tmp, api, "title", "demo", "Т2") == 0
    err = capsys.readouterr().err
    assert "вне скрипта" in err
    st = pack_state(tmp)
    assert [i["custom_emoji_id"] for i in st["items"]] == ids(api)
    assert st["items"][-1]["src"] is None and st["items"][0]["src"]


def test_create_from_set_uses_source_emoji(work):
    api = FakeApi()
    tmp, files = work
    assert run(tmp, api, "create", "src", "--title", "S", "--kind", "emoji", "--emoji", "🦀",
               "--files", *map(str, files[:3])) == 0
    # исходные файлы FakeApi.download отдаёт как фиктивный WEBM — проверяем лишь маршрут выбора и ошибки
    assert run(tmp, api, "create", "copy", "--title", "C", "--kind", "emoji",
               "--from-set", "src_by_testbot:9") == 1


def test_preview_builds_page_and_sheet(work, capsys):
    api = FakeApi()
    tmp, files = make_pack(work, api, 3)
    capsys.readouterr()
    assert run(tmp, api, "preview", "demo") == 0
    d = tmp / "state" / "demo_by_testbot" / "preview"
    page = (d / "index.html").read_text()
    assert page.count("<figure>") == 3 and "<img" in page and "dark" in page and "viewport" in page
    assert (d / "01.webp").is_file() and oct((d / "01.webp").stat().st_mode & 0o777) == "0o644"
    assert not (d / "01.webp").is_symlink()
    from PIL import Image
    with Image.open(d / "sheet.png") as im:
        assert im.size == sp.sheet_size(3, 3)
    out = capsys.readouterr().out
    assert str(d / "sheet.png") in out and "отправьте" in out
    assert "stand" not in out.lower() and "/s/" not in out


def test_preview_custom_out_new_dir_is_marked_and_rerun_replaces_own_files(work):
    api = FakeApi()
    tmp, files = make_pack(work, api, 2)
    out = tmp / "elsewhere" / "pv"
    assert run(tmp, api, "preview", "demo", "--out", str(out)) == 0
    assert (out / sp.PREVIEW_MARKER).is_file() and (out / "sheet.png").is_file() and (out / "02.webp").is_file()
    (out / "09.webp").write_bytes(b"stale")
    (out / "keep.txt").write_bytes(b"mine")
    assert run(tmp, api, "preview", "demo", "--out", str(out)) == 0
    assert not (out / "09.webp").exists() and (out / "keep.txt").read_bytes() == b"mine"


def test_preview_custom_out_empty_existing_dir_is_claimed(work):
    api = FakeApi()
    tmp, files = make_pack(work, api, 2)
    out = tmp / "empty"
    out.mkdir()
    assert run(tmp, api, "preview", "demo", "--out", str(out)) == 0
    assert (out / sp.PREVIEW_MARKER).is_file() and (out / "index.html").is_file()


def test_preview_custom_out_foreign_dir_is_error_and_untouched(work, capsys):
    api = FakeApi()
    tmp, files = make_pack(work, api, 2)
    out = tmp / "elsewhere"
    out.mkdir()
    (out / "keep.png").write_bytes(b"mine")
    (out / "05.webp").write_bytes(b"theirs")
    (out / "sheet.png").write_bytes(b"theirs too")
    capsys.readouterr()
    assert run(tmp, api, "preview", "demo", "--out", str(out)) == 1
    err = capsys.readouterr().err
    assert "не пуст" in err and "пустой или новый каталог" in err
    assert sorted(x.name for x in out.iterdir()) == ["05.webp", "keep.png", "sheet.png"]
    assert (out / "05.webp").read_bytes() == b"theirs" and (out / "sheet.png").read_bytes() == b"theirs too"


def test_preview_publish_into_foreign_dir_is_error(work, capsys):
    api = FakeApi()
    tmp, files = make_pack(work, api, 2)
    root = tmp / "www"
    (root / "page").mkdir(parents=True)
    (root / "page" / "01.webp").write_bytes(b"theirs")
    capsys.readouterr()
    assert run(tmp, api, "preview", "demo", "--publish", "page", "--publish-root", str(root)) == 1
    assert "пустой или новый каталог" in capsys.readouterr().err
    assert (root / "page" / "01.webp").read_bytes() == b"theirs"


def test_preview_symlink_dir_is_refused(work, capsys):
    api = FakeApi()
    tmp, files = make_pack(work, api, 2)
    target = tmp / "real"
    target.mkdir()
    link = tmp / "link"
    link.symlink_to(target, target_is_directory=True)
    capsys.readouterr()
    assert run(tmp, api, "preview", "demo", "--out", str(link)) == 1
    assert "символическая ссылка" in capsys.readouterr().err
    assert list(target.iterdir()) == []
    root = tmp / "www"
    root.mkdir()
    (root / "page").symlink_to(target, target_is_directory=True)
    assert run(tmp, api, "preview", "demo", "--publish", "page", "--publish-root", str(root)) == 1
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("publish", [False, True])
def test_preview_never_follows_file_symlinks(work, publish):
    api = FakeApi()
    tmp, files = make_pack(work, api, 2)
    outside = tmp / "outside"
    outside.mkdir()
    victims = {n: outside / n for n in ("a.txt", "b.png", "c.webp", "d.png")}
    for v in victims.values():
        v.write_bytes(b"precious")
    out = tmp / "www" / "page" if publish else tmp / "pv"
    out.mkdir(parents=True)
    (out / sp.PREVIEW_MARKER).write_text("x")
    (out / "index.html").symlink_to(victims["a.txt"])
    (out / "sheet.png").symlink_to(victims["b.png"])
    (out / "01.webp").symlink_to(victims["c.webp"])
    (out / "02.webp").symlink_to(tmp / "nowhere.webp")  # висячая ссылка
    argv = (["--publish", "page", "--publish-root", str(tmp / "www")] if publish else ["--out", str(out)])
    assert run(tmp, api, "preview", "demo", *argv) == 0
    assert all(v.read_bytes() == b"precious" for v in victims.values())
    for n in ("index.html", "sheet.png", "01.webp", "02.webp"):
        assert (out / n).is_file() and not (out / n).is_symlink()
    assert "<figure>" in (out / "index.html").read_text()


def test_preview_default_dir_without_marker_is_adopted(work):
    api = FakeApi()
    tmp, files = make_pack(work, api, 2)
    d = tmp / "state" / "demo_by_testbot" / "preview"
    d.mkdir(parents=True)
    (d / "07.webp").write_bytes(b"old")
    assert run(tmp, api, "preview", "demo") == 0
    assert (d / sp.PREVIEW_MARKER).is_file() and not (d / "07.webp").exists()


def test_preview_item_without_source_gets_error_tile(work, capsys):
    api = FakeApi()
    tmp, files = make_pack(work, api, 2)
    p = tmp / "state" / "demo_by_testbot" / "pack.json"
    st = json.loads(p.read_text())
    st["items"][1]["src"] = None  # потеряли исходник: preview скачает его (в FakeApi это битый webm)
    p.write_text(json.dumps(st))
    capsys.readouterr()
    assert run(tmp, api, "preview", "demo") == 0
    assert "не удалось построить превью" in capsys.readouterr().err
    assert (tmp / "state" / "demo_by_testbot" / "preview" / "02.webm").is_file()


def test_preview_publish_to_temp_root(work, capsys):
    api = FakeApi()
    tmp, files = make_pack(work, api, 3)
    root = tmp / "www"
    root.mkdir()
    capsys.readouterr()
    assert run(tmp, api, "preview", "demo", "--publish", "my-page", "--publish-root", str(root),
               "--url-base", "https://example.test/p/") == 0
    d = root / "my-page"
    assert (d / "index.html").is_file() and (d / "01.webp").is_file() and (d / "sheet.png").is_file()
    assert not (d / "01.webp").is_symlink()
    assert oct((d / "01.webp").stat().st_mode & 0o777) == "0o644" and oct(d.stat().st_mode & 0o777) == "0o755"
    assert "https://example.test/p/my-page/" in capsys.readouterr().out
    assert pack_state(tmp)["preview_name"] == "my-page"
    # повтор без имени берёт сохранённое; лишние старые файлы исчезают
    (d / "09.webp").write_bytes(b"stale")
    assert run(tmp, api, "preview", "demo", "--publish", "--publish-root", str(root)) == 0
    assert not (d / "09.webp").exists() and not (root / "demo").exists()
    assert run(tmp, api, "preview", "demo", "--publish", "bad name", "--publish-root", str(root)) == 1


def test_preview_publish_uses_config_and_requires_root(work, capsys):
    api = FakeApi()
    tmp, files = make_pack(work, api, 2)
    capsys.readouterr()
    assert run(tmp, api, "preview", "demo", "--publish", "x") == 1
    assert "публикация не настроена" in capsys.readouterr().err
    root = tmp / "www"
    sp.save_config(tmp / "state", {"preview_root": str(root), "preview_url_base": "https://e.test/s"})
    assert run(tmp, api, "preview", "demo", "--publish") == 0  # имя по умолчанию — без _by_<бот>
    assert (root / "demo" / "index.html").is_file()
    assert "https://e.test/s/demo/" in capsys.readouterr().out


def test_stand_dir_migrates_to_preview_name(work):
    api = FakeApi()
    tmp, files = make_pack(work, api, 2)
    p = tmp / "state" / "demo_by_testbot" / "pack.json"
    st = json.loads(p.read_text())
    st["stand_dir"] = "legacy"
    p.write_text(json.dumps(st))
    root = tmp / "www"
    assert run(tmp, api, "preview", "demo", "--publish", "--publish-root", str(root)) == 0
    assert (root / "legacy" / "index.html").is_file()
    st = pack_state(tmp)
    assert st["preview_name"] == "legacy" and "stand_dir" not in st
    assert run(tmp, api, "title", "demo", "Т2") == 0
    st = pack_state(tmp)
    assert st["preview_name"] == "legacy" and "stand_dir" not in st


def test_stand_command_removed():
    with pytest.raises(SystemExit):
        sp.main(["stand", "demo"], api=FakeApi())


def test_delete_set_requires_exact_confirmation(work):
    api = FakeApi()
    tmp, files = make_pack(work, api, 2)
    assert run(tmp, api, "delete-set", "demo", "--confirm", "demo") == 1
    assert "demo_by_testbot" in api.sets
    assert run(tmp, api, "delete-set", "demo", "--confirm", "demo_by_testbot") == 0
    assert api.sets == {}


def test_identify_with_zip(tmp_path):
    import zipfile
    z = tmp_path / "pack.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for fid in VECTORS:
            zf.writestr(f"{fid}/{fid}.webm", b"x")
    api = FakeApi()
    api.sets["Src"] = {"title": "Src", "type": "custom_emoji", "stickers": [
        {"file_id": "a", "file_unique_id": "a", "custom_emoji_id": str(v), "emoji": "💬"}
        for v in reversed(list(VECTORS.values()))]}
    orig = api.call

    def call(method, params=None, files=None):
        if method == "getCustomEmojiStickers":
            return [{"custom_emoji_id": i, "set_name": "Src", "emoji": "💬"} for i in params["custom_emoji_ids"]]
        return orig(method, params, files)

    api.call = call
    assert sp.collect_file_ids([str(z)]) == list(VECTORS)
    assert run(tmp_path, api, "identify", str(z)) == 0


# ------------------------------------------------------------------ правки 2

def test_create_new_pack_has_no_false_warning(work, capsys):
    api = FakeApi()
    tmp, files = work
    assert run(tmp, api, "create", "demo", "--title", "T", "--kind", "emoji", "--files", *map(str, files[:2])) == 0
    err = capsys.readouterr().err
    assert "pack.json не было" not in err and "вне скрипта" not in err
    assert all(i["src"] for i in pack_state(tmp)["items"])


def test_missing_pack_json_warns_only_for_unknown_src(work, capsys):
    api = FakeApi()
    tmp, files = make_pack(work, api, 3)
    (tmp / "state" / "demo_by_testbot" / "pack.json").unlink()
    capsys.readouterr()
    assert run(tmp, api, "title", "demo", "Т2") == 0
    err = capsys.readouterr().err
    assert "pack.json не было" in err and "3 элемент" in err
    assert all(i["src"] is None for i in pack_state(tmp)["items"])


def test_sync_state_fresh_with_adopt_is_silent():
    remote = {"name": "x_by_b", "title": "X", "sticker_type": "custom_emoji", "stickers": [
        {"file_unique_id": "u1", "custom_emoji_id": "1", "emoji": "a"}]}
    state, warns = sp.sync_state(None, remote, adopt=[{"src": "/f", "source": "file:f", "src_md5": "m"}])
    assert warns == [] and state["items"][0]["src"] == "/f"
    _, warns = sp.sync_state(None, remote)
    assert len(warns) == 1


def test_short_name_of_our_pack_in_read_commands(work, capsys):
    api = FakeApi()
    tmp, files = make_pack(work, api, 3)
    capsys.readouterr()
    assert run(tmp, api, "info", "demo") == 0
    assert "demo_by_testbot" in capsys.readouterr().out
    out = tmp / "fetched"
    assert run(tmp, api, "fetch", "demo:1-2", "--out", str(out)) == 0
    assert json.loads((out / "meta.json").read_text())["set"] == "demo_by_testbot"
    capsys.readouterr()
    # FakeApi отдаёт фиктивный WEBM: набор найден по короткому имени, ошибка уже от проверки файла
    assert run(tmp, api, "create", "copy", "--title", "C", "--kind", "emoji", "--dry-run",
               "--from-set", "demo:1") == 1
    err = capsys.readouterr().err
    assert "demo_by_testbot_001" in err and "STICKERSET_INVALID" not in err
    assert run(tmp, api, "info", "nonexistent") == 1


def test_foreign_pack_without_suffix_unchanged(work):
    api = FakeApi()
    tmp, files = work
    api.sets["AIByVolodya"] = {"title": "Чужой", "type": "custom_emoji", "stickers": [
        {"file_id": "a", "file_unique_id": "a", "custom_emoji_id": "5", "emoji": "x"}]}
    assert run(tmp, api, "info", "AIByVolodya") == 0
    assert api.log.count("getStickerSet") == 1 and "getMe" not in api.log
    assert run(tmp, api, "info", "https://t.me/addemoji/AIByVolodya") == 0


def test_preview_name_is_remembered_and_changeable(work):
    api = FakeApi()
    tmp, files = make_pack(work, api, 2)
    root = tmp / "www"
    args = ("--publish-root", str(root))
    assert run(tmp, api, "preview", "demo", "--publish", "custom", *args) == 0
    assert pack_state(tmp)["preview_name"] == "custom"
    assert run(tmp, api, "preview", "demo", "--publish", *args) == 0
    assert (root / "custom" / "index.html").is_file() and not (root / "demo").exists()
    assert run(tmp, api, "preview", "demo", "--publish", "other", *args) == 0
    assert pack_state(tmp)["preview_name"] == "other"
    assert run(tmp, api, "preview", "demo", "--publish", "bad dir", *args) == 1
    assert pack_state(tmp)["preview_name"] == "other"


def test_preview_without_publish_does_not_store_name(work):
    api = FakeApi()
    tmp, files = make_pack(work, api, 2)
    assert run(tmp, api, "preview", "demo") == 0
    assert "preview_name" not in pack_state(tmp)


def test_sync_state_keeps_foreign_top_level_fields(work):
    api = FakeApi()
    tmp, files = make_pack(work, api, 2)
    p = tmp / "state" / "demo_by_testbot" / "pack.json"
    st = json.loads(p.read_text())
    st.update({"preview_name": "keep", "note": {"a": 1}})
    p.write_text(json.dumps(st))
    assert run(tmp, api, "title", "demo", "Новое") == 0
    assert run(tmp, api, "add", "demo", "--files", str(files[3])) == 0
    st = pack_state(tmp)
    assert st["preview_name"] == "keep" and st["note"] == {"a": 1} and st["title"] == "Новое"


# ------------------------------------------------- токен, владелец, каталог (задание 4)

def dotenv(path, **kv):
    path.write_text("".join(f"{k}={v}\n" for k, v in kv.items()))
    return str(path)


def test_token_order_flag_env_config(tmp_path):
    flag_file = dotenv(tmp_path / "flag.env", STICKER_BOT_TOKEN="1:flag")
    cfg_file = dotenv(tmp_path / "cfg.env", STICKER_BOT_TOKEN="3:config")
    config = {"token_env_file": cfg_file}
    env = {"STICKER_BOT_TOKEN": "2:env"}
    assert sp.resolve_token(flag_file, None, config, env) == "1:flag"
    assert sp.resolve_token(None, None, config, env) == "2:env"
    assert sp.resolve_token(None, None, config, {}) == "3:config"
    assert sp.resolve_token(None, None, {}, {"STICKER_BOT_TOKEN": "  7:pad \n"}) == "7:pad"


def test_token_var_and_fallback_names(tmp_path):
    both = dotenv(tmp_path / "a.env", TELEGRAM_BOT_TOKEN="1:tg", STICKER_BOT_TOKEN="2:st", CUSTOM="3:cu")
    only_tg = dotenv(tmp_path / "b.env", TELEGRAM_BOT_TOKEN="4:tg")
    assert sp.resolve_token(both, env={}) == "2:st"  # STICKER_BOT_TOKEN приоритетнее
    assert sp.resolve_token(only_tg, env={}) == "4:tg"  # затем TELEGRAM_BOT_TOKEN
    assert sp.resolve_token(both, "CUSTOM", env={}) == "3:cu"
    with pytest.raises(sp.CliError) as e:
        sp.resolve_token(both, "MISSING", env={})
    assert "MISSING" in str(e.value)
    cfg = {"token_env_file": both, "token_var": "CUSTOM"}
    assert sp.resolve_token(None, None, cfg, {}) == "3:cu"
    assert sp.resolve_token(None, "TELEGRAM_BOT_TOKEN", cfg, {}) == "1:tg"


def test_token_file_errors_do_not_fall_through(tmp_path):
    with pytest.raises(sp.CliError) as e:
        sp.resolve_token(str(tmp_path / "nope"), env={"STICKER_BOT_TOKEN": "9:env"})
    assert "не удалось прочитать" in str(e.value)


def test_no_token_gives_russian_instructions(capsys):
    assert sp.main(["info", "x"]) == 1
    err = capsys.readouterr().err
    for word in ("@BotFather", "Старт", "STICKER_BOT_TOKEN", "Пользовательские ключи", "$HERMES_HOME", "в чат"):
        assert word in err


def test_token_from_env_used_by_main_and_not_printed(monkeypatch, capsys):
    monkeypatch.setenv("STICKER_BOT_TOKEN", TOKEN)
    seen = {}

    def fake(req, timeout=None):
        seen["url"] = req.full_url
        raise urllib.error.URLError(req.full_url)

    monkeypatch.setattr(sp.urllib.request, "urlopen", fake)
    assert sp.main(["whoami"]) == 1
    assert TOKEN in seen["url"]
    out = capsys.readouterr()
    assert TOKEN not in out.out + out.err


def test_owner_order_flag_env_config():
    cfg = {"owner_id": 3}
    assert sp.resolve_owner(1, cfg, {"STICKER_OWNER_ID": "2"}) == (1, "флаг --owner")
    assert sp.resolve_owner(None, cfg, {"STICKER_OWNER_ID": "2"})[0] == 2
    assert sp.resolve_owner(None, cfg, {})[0] == 3
    assert sp.resolve_owner(None, {}, {}) == (None, "")
    with pytest.raises(sp.CliError):
        sp.resolve_owner(None, {}, {"STICKER_OWNER_ID": "abc"})


def test_missing_owner_is_error_with_setup_hint(work, capsys):
    tmp, files = work
    api = FakeApi()
    assert sp.main(["create", "demo", "--title", "T", "--kind", "emoji", "--files", str(files[0])],
                   api=api, base=tmp / "state") == 1
    assert "setup" in capsys.readouterr().err
    assert "uploadStickerFile" not in api.log
    # чтение не требует владельца
    api.sets["x"] = {"title": "X", "type": "regular", "stickers": []}
    assert sp.main(["info", "x"], api=api, base=tmp / "state") == 0


def test_owner_from_env_and_config_reach_upload(work, monkeypatch):
    tmp, files = work
    seen = []
    api = FakeApi()
    orig = api.call
    api.call = lambda m, params=None, files=None: (seen.append(params.get("user_id")) if m == "uploadStickerFile" else None) or orig(m, params, files)
    monkeypatch.setenv("STICKER_OWNER_ID", "555")
    assert sp.main(["create", "demo", "--title", "T", "--kind", "emoji", "--files", str(files[0])],
                   api=api, base=tmp / "state") == 0
    monkeypatch.delenv("STICKER_OWNER_ID")
    sp.save_config(tmp / "state", {"owner_id": 777})
    assert sp.main(["add", "demo", "--files", str(files[1])], api=api, base=tmp / "state") == 0
    assert seen == [555, 777]


def test_home_dir_order(monkeypatch, tmp_path):
    assert sp.home_dir({"STICKERPACKS_HOME": "/x/h", "HERMES_HOME": "/y"}) == Path("/x/h")
    assert sp.home_dir({"HERMES_HOME": "/y/hermes"}) == Path("/y/hermes/sticker-packs")
    monkeypatch.setenv("HOME", str(tmp_path / "u"))
    assert sp.home_dir({}) == tmp_path / "u" / ".sticker-packs"


def test_main_uses_home_for_state_and_config(work, monkeypatch):
    tmp, files = work
    home = tmp / "customhome"
    monkeypatch.setenv("STICKERPACKS_HOME", str(home))
    sp.save_config(home, {"owner_id": 5})
    api = FakeApi()
    assert sp.main(["create", "demo", "--title", "T", "--kind", "emoji", "--files", str(files[0])], api=api) == 0
    assert (home / "demo_by_testbot" / "pack.json").is_file()


def test_corrupt_config_is_clear_error(tmp_path, capsys):
    home = tmp_path / "state"
    home.mkdir()
    (home / "config.json").write_text("{oops")
    assert sp.main(["whoami"], api=FakeApi(), base=home) == 1
    assert "config.json" in capsys.readouterr().err


# ------------------------------------------------------------------- setup

def upd(uid, text="/start", first="Анна", username="anna", chat_type="private", is_bot=False, update_id=None,
        date=1_759_550_000):
    return {"update_id": update_id or uid,
            "message": {"text": text, "date": date, "chat": {"id": uid, "type": chat_type},
                        "from": {"id": uid, "first_name": first, "username": username, "is_bot": is_bot}}}


def setup_args(tmp, *extra):
    return ["setup", *extra]


def test_setup_finds_owner_from_updates(tmp_path, capsys):
    api = FakeApi()
    api.updates = [upd(22, text="привет"), upd(33, chat_type="group"), upd(44, is_bot=True), upd(22)]
    home = tmp_path / "state"
    assert sp.main(["setup"], api=api, base=home) == 0
    out = capsys.readouterr().out
    assert "@testbot" in out and "Анна" in out and "@anna" in out and "22" in out
    assert json.loads((home / "config.json").read_text()) == {"owner_id": 22}
    assert "getUpdates" in api.log and "getChat" in api.log
    assert "info" in out and "--dry-run" in out
    # после setup владелец берётся из config.json
    assert sp.main(["whoami"], api=api, base=home) == 0


def test_setup_several_people_is_not_saved_automatically(tmp_path, capsys):
    api = FakeApi()
    api.updates = [upd(111, text="/start", first="Владелец", username="owner1", date=1_759_550_000),
                   upd(222, text="привет, а это что за бот и зачем он нужен вообще", first="Борис",
                       username="boris", date=1_759_550_600)]
    home = tmp_path / "state"
    assert sp.main(["setup"], api=api, base=home) == 1
    err = capsys.readouterr().err
    assert "--owner" in err
    assert "id 111" in err and "Владелец" in err and "@owner1" in err and "последний /start" in err
    assert "id 222" in err and "Борис" in err and "@boris" in err and "2025-10-04" in err
    assert "привет, а это что за бот и зачем" not in err and "привет, а это что за бот и зач" in err  # до 30 символов
    assert err.index("id 111") < err.index("id 222")  # /start первым, хотя 222 писал позже
    assert "getChat" not in api.log and not (home / "config.json").exists()
    # повтор с явным id сохраняет
    assert sp.main(["setup", "--owner", "111"], api=api, base=home) == 0
    assert json.loads((home / "config.json").read_text())["owner_id"] == 111


def test_setup_start_priority_when_several_people(tmp_path, capsys):
    api = FakeApi()
    api.updates = [upd(1, text="hello", first="Первый", update_id=1), upd(2, text="/start", first="Второй", update_id=2),
                   upd(3, text="hi", first="Третий", update_id=3), upd(2, text="ещё", first="Второй", update_id=4)]
    assert sp.main(["setup"], api=api, base=tmp_path / "state") == 1
    err = capsys.readouterr().err
    assert err.index("id 2") < err.index("id 3") < err.index("id 1")
    assert err.count("последний /start") == 1 and err.index("последний /start") < err.index("id 3")


def test_setup_single_person_without_start_is_saved(tmp_path, capsys):
    api = FakeApi()
    api.updates = [upd(7, text="привет", update_id=1), upd(7, text="ещё раз", update_id=2),
                   upd(8, chat_type="supergroup"), upd(9, is_bot=True)]
    home = tmp_path / "state"
    assert sp.main(["setup"], api=api, base=home) == 0
    assert json.loads((home / "config.json").read_text())["owner_id"] == 7


def test_setup_no_messages_asks_to_press_start(tmp_path, capsys):
    api = FakeApi()
    home = tmp_path / "state"
    assert sp.main(["setup"], api=api, base=home) == 1
    assert "Старт" in capsys.readouterr().err
    assert not (home / "config.json").exists()


def test_setup_409_explains_busy_bot(tmp_path, capsys):
    api = FakeApi()
    api.updates_error = sp.ApiError("getUpdates", "Conflict: terminated by other getUpdates request", 409)
    assert sp.main(["setup"], api=api, base=tmp_path / "state") == 1
    err = capsys.readouterr().err
    assert "занят другой программой" in err and "--owner" in err and "отдельного бота" in err
    assert not (tmp_path / "state" / "config.json").exists()


def test_setup_explicit_owner_skips_updates(tmp_path):
    api = FakeApi()
    home = tmp_path / "state"
    assert sp.main(["setup", "--owner", "99"], api=api, base=home) == 0
    assert "getUpdates" not in api.log
    assert json.loads((home / "config.json").read_text())["owner_id"] == 99


def test_setup_keeps_existing_owner_unless_flag(tmp_path, monkeypatch, capsys):
    api = FakeApi()
    api.updates = [upd(5)]
    home = tmp_path / "state"
    sp.save_config(home, {"owner_id": 8, "preview_root": "/keep"})
    assert sp.main(["setup"], api=api, base=home) == 0
    assert "getUpdates" not in api.log
    cfg = json.loads((home / "config.json").read_text())
    assert cfg["owner_id"] == 8 and cfg["preview_root"] == "/keep"


def test_setup_unreachable_owner_not_saved(tmp_path, capsys):
    api = FakeApi()
    api.chat_error = "Bad Request: chat not found"
    assert sp.main(["setup", "--owner", "99"], api=api, base=tmp_path / "state") == 1
    assert "Старт" in capsys.readouterr().err
    assert not (tmp_path / "state" / "config.json").exists()


def test_setup_saves_token_env_file_path_but_never_token(tmp_path, capsys):
    env = dotenv(tmp_path / "bot.env", TELEGRAM_BOT_TOKEN=TOKEN)
    api = None
    # setup сам берёт токен из --token-env-file; сеть подменяем через FakeApi-подобный Api
    fake = FakeApi()
    home = tmp_path / "state"
    import stickerpack
    orig_api = stickerpack.Api
    stickerpack.Api = lambda token, *a, **k: fake  # noqa: E731
    try:
        assert sp.main(["setup", "--owner", "7", "--token-env-file", env, "--token-var", "TELEGRAM_BOT_TOKEN",
                        "--preview-root", str(tmp_path / "www"), "--preview-url-base", "https://e.test/"],
                       base=home) == 0
    finally:
        stickerpack.Api = orig_api
    text = (home / "config.json").read_text()
    cfg = json.loads(text)
    assert cfg["token_env_file"] == str(Path(env).resolve()) and cfg["token_var"] == "TELEGRAM_BOT_TOKEN"
    assert cfg["preview_root"] == str(tmp_path / "www") and cfg["preview_url_base"] == "https://e.test/"
    out = capsys.readouterr()
    assert TOKEN not in text + out.out + out.err
    # дальше токен находится через config.json
    assert sp.resolve_token(None, None, cfg, {}) == TOKEN


# -------------------------------------------------------------- getFile, cover

class NoPathApi(FakeApi):
    download = sp.Api.download

    def call(self, method, params=None, files=None):
        if method == "getFile":
            return {}
        return super().call(method, params, files)


def test_download_without_file_path_is_clear_error():
    with pytest.raises(sp.CliError) as e:
        NoPathApi().download("fid")
    assert "Telegram не отдал файл" in str(e.value) and "20 МБ" in str(e.value)


def test_preview_without_getfile_path_has_no_traceback(work, capsys):
    tmp, files = work
    api = NoPathApi()
    make_pack(work, api, 2)
    p = tmp / "state" / "demo_by_testbot" / "pack.json"
    st = json.loads(p.read_text())
    st["items"][0]["src"] = None
    p.write_text(json.dumps(st))
    capsys.readouterr()
    assert run(tmp, api, "preview", "demo") == 1
    err = capsys.readouterr().err
    assert "Telegram не отдал файл" in err and "KeyError" not in err and TOKEN not in err


@pytest.fixture
def sticker_pack(tmp_path):
    from PIL import Image
    src = tmp_path / "s.png"
    Image.new("RGBA", (512, 300), (200, 0, 0, 255)).save(src)
    api = FakeApi()
    assert run(tmp_path, api, "create", "st", "--title", "S", "--kind", "sticker", "--files", str(src)) == 0
    api.log.clear()
    return tmp_path, api


def cover(tmp, api, path):
    return run(tmp, api, "cover", "st", "--file", str(path))


def test_cover_static_ok_png_and_webp(sticker_pack):
    from PIL import Image
    tmp, api = sticker_pack
    for ext in ("png", "webp"):
        p = tmp / f"c.{ext}"
        Image.new("RGBA", (100, 100), (0, 0, 255, 255)).save(p)
        assert cover(tmp, api, p) == 0
    assert api.log.count("setStickerSetThumbnail") == 2


def test_cover_static_violations_listed_before_api(sticker_pack, capsys, monkeypatch):
    from PIL import Image
    tmp, api = sticker_pack
    p = tmp / "big.png"
    Image.new("RGBA", (512, 512), (0, 0, 0, 255)).save(p)
    capsys.readouterr()
    assert cover(tmp, api, p) == 1
    assert "100×100" in capsys.readouterr().err
    q = tmp / "noise.png"
    Image.frombytes("RGB", (100, 100), os.urandom(100 * 100 * 3)).save(q)
    monkeypatch.setattr(sp, "COVER_STATIC_BYTES", 128 * 1024 // 8)  # 100×100 не весит 128 КБ — сужаем лимит
    assert q.stat().st_size > sp.COVER_STATIC_BYTES
    assert cover(tmp, api, q) == 1
    assert "128 КБ" in capsys.readouterr().err
    j = tmp / "c.jpg"
    Image.new("RGB", (100, 100)).save(j)
    assert cover(tmp, api, j) == 1
    assert "формат .jpg" in capsys.readouterr().err
    assert "setStickerSetThumbnail" not in api.log


def test_cover_tgs_limits_and_magic(sticker_pack, capsys):
    tmp, api = sticker_pack
    ok = tmp / "ok.tgs"
    ok.write_bytes(gzip.compress(b"{}"))
    assert cover(tmp, api, ok) == 0
    big = tmp / "big.tgs"
    big.write_bytes(b"\x1f\x8b" + os.urandom(40 * 1024))
    bad = tmp / "bad.tgs"
    bad.write_bytes(b"not gzip")
    capsys.readouterr()
    assert cover(tmp, api, big) == 1 and "32 КБ" in capsys.readouterr().err
    assert cover(tmp, api, bad) == 1 and "gzip" in capsys.readouterr().err
    assert api.log.count("setStickerSetThumbnail") == 1


def make_webm(path, size, codec="libvpx-vp9", seconds=1):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=red:s={size}:d={seconds}:r=10",
                    "-c:v", codec, str(path)], check=True)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="нужен ffmpeg")
def test_cover_webm_checks_codec_size_and_bytes(sticker_pack, capsys):
    tmp, api = sticker_pack
    ok = tmp / "ok.webm"
    make_webm(ok, "100x100")
    assert ok.stat().st_size <= 32 * 1024
    assert cover(tmp, api, ok) == 0
    wrong = tmp / "wrong.webm"
    make_webm(wrong, "128x128")
    capsys.readouterr()
    assert cover(tmp, api, wrong) == 1 and "128×128" in capsys.readouterr().err
    vp8 = tmp / "vp8.webm"
    make_webm(vp8, "100x100", codec="libvpx")
    assert cover(tmp, api, vp8) == 1 and "VP9" in capsys.readouterr().err
    assert api.log.count("setStickerSetThumbnail") == 1


def test_cover_file_not_accepted_for_emoji_pack(work, capsys):
    from PIL import Image
    api = FakeApi()
    tmp, files = make_pack(work, api, 2)
    p = tmp / "c.png"
    Image.new("RGBA", (100, 100)).save(p)
    capsys.readouterr()
    assert run(tmp, api, "cover", "demo", "--file", str(p)) == 1
    err = capsys.readouterr().err
    assert "--file не принимается" in err and "--pos" in err
    assert "setCustomEmojiStickerSetThumbnail" not in api.log and "setStickerSetThumbnail" not in api.log
    assert run(tmp, api, "cover", "demo", "--pos", "2") == 0


def test_cover_sticker_pack_needs_file(sticker_pack, capsys):
    tmp, api = sticker_pack
    capsys.readouterr()
    assert run(tmp, api, "cover", "st", "--pos", "1") == 1
    assert "--file" in capsys.readouterr().err


# ------------------------------------------------------------------- sheet

def make_pngs(tmp_path, n, size=(100, 100)):
    from PIL import Image
    d = tmp_path / "pics"
    d.mkdir(exist_ok=True)
    paths = []
    for i in range(1, n + 1):
        p = d / f"{i:02d}.png"
        Image.new("RGBA", size, (200, 30 * i % 255, 40, 255)).save(p)
        paths.append(p)
    return d, paths


def test_sheet_grid_numbers_and_dark(tmp_path, capsys):
    from PIL import Image
    d, paths = make_pngs(tmp_path, 7)
    out = tmp_path / "sheet.png"
    assert sp.main(["sheet", str(d), "--out", str(out), "--cols", "3"]) == 0  # токен не нужен
    with Image.open(out) as im:
        assert im.size == sp.sheet_size(7, 3) and im.size[1] > sp.TILE_H * 2
        light = im.convert("RGB")
        assert light.getpixel((2, 2)) == (255, 255, 255)
        # картинка в центре плитки, в подписи под ней нарисован номер (тёмные пиксели)
        for i in range(7):
            x, y = sp.tile_origin(i, 3)
            assert light.getpixel((x + sp.TILE_W // 2, y + 10 + sp.TILE_IMG // 2))[0] > 150
            label = light.crop((x + 20, y + sp.TILE_IMG + 14, x + sp.TILE_W - 20, y + sp.TILE_H - 10))
            assert min(label.convert('L').tobytes()) < 80
    text = capsys.readouterr().out
    assert "   7  07.png" in text and "отправьте эту картинку" in text
    dark = tmp_path / "dark.png"
    assert sp.main(["sheet", *map(str, paths[:2]), "--out", str(dark), "--dark"]) == 0
    with Image.open(dark) as im:
        assert im.convert("RGB").getpixel((2, 2)) == (23, 33, 43)
        assert im.size == sp.sheet_size(2, 2)  # колонок не больше, чем плиток


def test_sheet_splits_into_pages_with_global_numbers(tmp_path, capsys):
    d, paths = make_pngs(tmp_path, 5, size=(20, 20))
    out = tmp_path / "s" / "sheet.png"
    assert sp.main(["sheet", str(d), "--out", str(out), "--per-sheet", "2"]) == 0
    assert out.is_file() and (tmp_path / "s" / "sheet_2.png").is_file() and (tmp_path / "s" / "sheet_3.png").is_file()
    assert "   5  05.png" in capsys.readouterr().out


def test_sheet_errors(tmp_path, capsys):
    assert sp.main(["sheet", str(tmp_path / "nope"), "--out", str(tmp_path / "s.png")]) == 1
    (tmp_path / "empty").mkdir()
    assert sp.main(["sheet", str(tmp_path / "empty"), "--out", str(tmp_path / "s.png")]) == 1
    d, _ = make_pngs(tmp_path, 1)
    assert sp.main(["sheet", str(d), "--out", str(tmp_path / "s.txt")]) == 1


def make_alpha_webm(path, empty_frames=5, total=10):
    from PIL import Image
    frames = path.parent / (path.stem + "_frames")
    frames.mkdir()
    for i in range(total):
        im = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
        if i >= empty_frames:
            im.paste(Image.new("RGBA", (60, 60), (255, 0, 0, 255)), (20, 20))
        im.save(frames / f"f_{i}.png")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-framerate", "10", "-i", str(frames / "f_%d.png"),
                    "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", str(path)], check=True)


def test_sheet_video_skips_transparent_first_frame(tmp_path):
    from PIL import Image
    webm = tmp_path / "clip.webm"
    make_alpha_webm(webm)
    frame = sp.tile_picture(webm)
    assert frame.getpixel((50, 50))[3] == 255 and frame.getpixel((50, 50))[0] > 200  # красный квадрат из середины
    assert frame.getpixel((2, 2))[3] == 0
    out = tmp_path / "sheet.png"
    assert sp.main(["sheet", str(webm), "--out", str(out)]) == 0
    with Image.open(out) as im:
        x, y = sp.tile_origin(0, 1)
        assert im.convert("RGB").getpixel((x + sp.TILE_W // 2, y + 10 + sp.TILE_IMG // 2))[0] > 200


def test_sheet_video_with_content_uses_first_frame(tmp_path):
    webm = tmp_path / "solid.webm"
    make_alpha_webm(webm, empty_frames=0)
    frame = sp.tile_picture(webm)
    assert frame.getpixel((50, 50))[0] > 200


def test_sheet_tgs_placeholder_and_animated_gif(tmp_path):
    import gzip
    from PIL import Image
    tgs = tmp_path / "a.tgs"
    tgs.write_bytes(gzip.compress(b"{}"))
    assert sp.tile_picture(tgs) is None
    gif = tmp_path / "g.gif"
    first = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
    second = Image.new("RGBA", (40, 40), (0, 200, 0, 255))
    first.save(gif, save_all=True, append_images=[second, second], duration=100, loop=0, disposal=2)
    pic = sp.tile_picture(gif)  # ffmpeg не нужен
    assert sp.content_pixels(pic) > 0
    out = tmp_path / "s.png"
    assert sp.main(["sheet", str(tgs), str(gif), "--out", str(out)]) == 0


# ------------------------------------------------------- без ffmpeg и Pillow

def test_static_flows_work_without_ffmpeg(work, monkeypatch, capsys):
    tmp, files = work
    from PIL import Image
    empty = tmp / "emptybin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    src = tmp / "pic.png"
    Image.new("RGBA", (300, 200), (255, 0, 0, 255)).save(src)
    rc, out = run_prepare(tmp, [src], "emoji")
    assert rc == 0 and sp.main(["validate", str(out / "pic.webp"), "--kind", "emoji"]) == 0
    api = FakeApi()
    assert run(tmp, api, "create", "demo", "--title", "T", "--kind", "emoji", "--files", str(out / "pic.webp")) == 0
    assert run(tmp, api, "add", "demo", "--files", str(files[0])) == 0
    assert run(tmp, api, "info", "demo") == 0 and run(tmp, api, "preview", "demo") == 0
    assert sp.main(["sheet", str(out / "pic.webp"), "--out", str(tmp / "s.png")]) == 0
    capsys.readouterr()
    # видео — понятная ошибка с подсказкой установки
    clip = tmp / "clip.webm"
    clip.write_bytes(b"x")
    assert sp.main(["validate", str(clip), "--kind", "emoji"]) == 1
    assert "ffmpeg" in capsys.readouterr().err
    assert sp.main(["sheet", str(clip), "--out", str(tmp / "v.png")]) == 1
    err = capsys.readouterr().err
    assert "ffmpeg" in err and "apt install ffmpeg" in err
    mp4 = tmp / "c.mp4"
    mp4.write_bytes(b"x")
    rc, _ = run_prepare(tmp, [mp4], "emoji")
    assert rc == 1 and "ffmpeg" in capsys.readouterr().out


def test_missing_pillow_message(monkeypatch, tmp_path, capsys):
    import builtins
    real = builtins.__import__

    def fake(name, *a, **k):
        if name == "PIL" or name.startswith("PIL."):
            raise ImportError("no PIL")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    p = tmp_path / "a.png"
    p.write_bytes(b"x")
    assert sp.main(["validate", str(p), "--kind", "emoji"]) == 1
    assert "pip install pillow" in capsys.readouterr().err
