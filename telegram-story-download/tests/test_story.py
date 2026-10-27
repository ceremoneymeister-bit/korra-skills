"""Без сети: границы запроса, видимый просмотр, запрет повторов и нового входа."""
import asyncio
from datetime import datetime, timezone
import fcntl
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from telethon.errors import FloodWaitError
from telethon.tl.types import InputPeerUser, MessageMediaPhoto, StoryItem, StoryItemDeleted

spec = importlib.util.spec_from_file_location("story", Path(__file__).parents[1] / "scripts/story.py")
story = importlib.util.module_from_spec(spec)
spec.loader.exec_module(story)


def args(link=None, peer=None, id=None, out="./stories"):
    return SimpleNamespace(link=link, peer=peer, id=id, out=out)


class FakeClient:
    def __init__(self, items=None, fail=None, views=True):
        self.events = []
        self.fail = fail
        self.views = views
        self.items = items if items is not None else [StoryItem(
            id=7, date=datetime(2026, 1, 1, tzinfo=timezone.utc),
            expire_date=datetime(2026, 1, 2, tzinfo=timezone.utc),
            media=MessageMediaPhoto(), caption="caption")]
        self.connect = AsyncMock()
        self.disconnect = AsyncMock()
        self.is_user_authorized = AsyncMock(return_value=True)
        self.get_me = AsyncMock(return_value=SimpleNamespace(id=42))

    async def get_input_entity(self, value):
        self.events.append(("peer", value))
        return InputPeerUser(42, 0)

    async def __call__(self, request):
        name = type(request).__name__
        self.events.append((name, getattr(request, "id", getattr(request, "max_id", None))))
        if name == self.fail:
            raise FloodWaitError(request=request, capture=33)
        if name == "GetStoriesByIDRequest":
            return SimpleNamespace(stories=self.items, chats=[], users=[SimpleNamespace(
                id=42, first_name="Example", last_name="User", username="example")])
        return self.views if name == "IncrementStoryViewsRequest" else [7]

    async def download_media(self, media, file):
        self.events.append(("download", media))
        if self.fail == "download":
            raise FloodWaitError(request=None, capture=33)
        Path(file + ".jpg").write_bytes(b"fixture")
        return file + ".jpg"


class TargetTests(unittest.TestCase):
    def test_link_and_explicit_target(self):
        self.assertEqual(story.target(args(link="https://t.me/example/s/7")), ("example", 7))
        self.assertEqual(story.target(args(link="https://t.me/example/s/7/")), ("example", 7))
        self.assertEqual(story.target(args(peer="@example", id=7)), ("example", 7))
        self.assertEqual(story.target(args(peer="42", id=7)), (42, 7))

    def test_rejects_ambiguous_or_invalid_input(self):
        cases = [args(), args(peer="example"), args(peer="example", id=0),
                 args(peer="example", id=-1), args(peer="https://t.me/example", id=7),
                 args(link="https://t.me/example/s/7", peer="other", id=9),
                 args(link="https://evil.test/example/s/7"),
                 args(link="https://t.me/example/7"), args(link="https://t.me/example/s/0")]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                story.target(case)


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.pace = patch.object(story, "pace", new_callable=AsyncMock).start()
        self.addCleanup(patch.stopall)

    async def test_marks_read_and_counts_selected_view_before_download(self):
        client = FakeClient()
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            path = await story.download_story(client, "example", 7, self.temp.name)
        self.assertEqual([e[0] for e in client.events], ["peer", "GetStoriesByIDRequest",
            "ReadStoriesRequest", "IncrementStoryViewsRequest", "download"])
        self.assertEqual(client.events[1:4], [
            ("GetStoriesByIDRequest", [7]), ("ReadStoriesRequest", 7),
            ("IncrementStoryViewsRequest", [7])])
        self.assertEqual(Path(path).read_bytes(), b"fixture")
        for expected in ("Example User", "2026-01-01", "MessageMediaPhoto", "caption", "Сохранено:"):
            self.assertIn(expected, output.getvalue())
        self.assertEqual(self.pace.await_count, 5)

    async def test_missing_deleted_or_wrong_id_has_no_read_or_download(self):
        for items in ([], [StoryItemDeleted(id=7)], [StoryItemDeleted(id=8)]):
            client = FakeClient(items)
            with self.assertRaisesRegex(ValueError, "недоступна"):
                await story.download_story(client, "example", 7, self.temp.name)
            self.assertEqual(len(client.events), 2)

    async def test_false_view_response_is_not_an_rpc_error(self):
        client = FakeClient(views=False)
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            path = await story.download_story(client, "example", 7, self.temp.name)
        self.assertTrue(Path(path).is_file())
        self.assertIn("Telegram: False", output.getvalue())
        self.assertEqual([e[0] for e in client.events].count("IncrementStoryViewsRequest"), 1)

    async def test_floodwait_at_each_stage_is_not_retried(self):
        stages = ["GetStoriesByIDRequest", "ReadStoriesRequest", "IncrementStoryViewsRequest", "download"]
        for stage in stages:
            client = FakeClient(fail=stage)
            with self.subTest(stage=stage), patch("sys.stdout", new_callable=io.StringIO), self.assertRaises(FloodWaitError):
                await story.download_story(client, "example", 7, self.temp.name)
            names = [e[0] for e in client.events]
            self.assertEqual(names[-1], stage)
            self.assertEqual(names.count(stage), 1)


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.session = root / "example.session"
        self.session.touch()
        self.fp = {k: "fixture" for k in ("device_model", "system_version", "app_version", "lang_code", "system_lang_code")}
        fp_file = root / "device.json"
        fp_file.write_text(json.dumps(self.fp))
        self.env = dict(TELEGRAM_API_ID="1", TELEGRAM_API_HASH="fixture", TELEGRAM_OWNER_ID="42",
                        TELEGRAM_SESSION_FILE=str(self.session), TELEGRAM_FINGERPRINT_FILE=str(fp_file))
        patch.object(story.os, "environ", self.env).start()
        patch.object(story, "pace", new_callable=AsyncMock).start()
        self.addCleanup(patch.stopall)

    async def test_wrong_owner_or_unauthorized_stops_before_story(self):
        for authorized, owner in ((False, 42), (True, 99)):
            client = FakeClient()
            client.is_user_authorized.return_value = authorized
            client.get_me.return_value = SimpleNamespace(id=owner)
            with patch("telethon.TelegramClient", return_value=client), self.assertRaises(ValueError):
                await story.run(args(peer="example", id=7))
            self.assertEqual(client.events, [])
            client.disconnect.assert_awaited_once()

    async def test_lock_prevents_client_creation(self):
        with open(str(self.session) + ".lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch("telethon.TelegramClient") as constructor, self.assertRaisesRegex(ValueError, "занята"):
                await story.run(args(peer="example", id=7))
            constructor.assert_not_called()

    async def test_fingerprint_and_no_retry_options_are_forwarded(self):
        client = FakeClient()
        with patch("telethon.TelegramClient", return_value=client) as constructor, \
                patch.object(story, "download_story", new_callable=AsyncMock):
            await story.run(args(peer="example", id=7))
        options = constructor.call_args.kwargs
        for key, value in self.fp.items():
            self.assertEqual(options[key], value)
        for key in ("receive_updates", "request_retries", "connection_retries", "flood_sleep_threshold", "auto_reconnect"):
            self.assertFalse(options[key])
        client.disconnect.assert_awaited_once()

    def test_missing_session_is_not_created(self):
        self.session.unlink()
        with self.assertRaisesRegex(ValueError, "существующий"):
            story.settings()
        self.assertFalse(self.session.exists())

    def test_incomplete_fingerprint_has_no_fallback(self):
        Path(self.env["TELEGRAM_FINGERPRINT_FILE"]).write_text('{"device_model":"fixture"}')
        with self.assertRaisesRegex(ValueError, "пять"):
            story.settings()


if __name__ == "__main__":
    unittest.main()
