import importlib
import importlib.util
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from datetime import datetime
from unittest import mock


SERVER_DIR = (
    Path(__file__).resolve().parents[1]
    / "whatsapp-mcp"
    / "whatsapp-mcp-server"
)
sys.path.insert(0, str(SERVER_DIR))

whatsapp = importlib.import_module("whatsapp")
audio = importlib.import_module("audio")
if importlib.util.find_spec("mcp"):
    mcp_main = importlib.import_module("main")
else:
    mcp_main = None


class WhatsAppReadToolsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "messages.db"
        with sqlite3.connect(self.db) as conn:
            conn.executescript(
                """
                CREATE TABLE chats (
                    jid TEXT PRIMARY KEY,
                    name TEXT,
                    last_message_time TEXT
                );
                CREATE TABLE messages (
                    id TEXT,
                    chat_jid TEXT,
                    sender TEXT,
                    content TEXT,
                    timestamp TEXT,
                    is_from_me BOOLEAN,
                    media_type TEXT,
                    PRIMARY KEY (id, chat_jid)
                );
                """
            )
            conn.executemany(
                "INSERT INTO chats VALUES (?, ?, ?)",
                [
                    ("direct@lid", "Direct", "2026-01-03 00:00:00"),
                    ("group@g.us", "Group", "2026-01-02 00:00:00"),
                ],
            )
            conn.executemany(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    ("a", "direct@lid", "contact@lid", "old", "2026-01-01 00:00:00", 0, None),
                    ("b", "direct@lid", "me", "latest stored", "2026-01-02 00:00:00", 1, None),
                    ("c", "direct@lid", "me", "latest tied by id", "2026-01-02 00:00:00", 1, None),
                    ("g1", "group@g.us", "contact@lid", "first", "2026-01-01 00:00:00", 0, None),
                    ("g2", "group@g.us", "contact@lid", "group latest", "2026-01-02 00:00:00", 0, None),
                ],
            )
        self.path_patch = mock.patch.object(whatsapp, "MESSAGES_DB_PATH", str(self.db))
        self.path_patch.start()

    def tearDown(self):
        self.path_patch.stop()
        self.tmp.cleanup()

    def test_last_message_is_deterministic_and_optional(self):
        without = whatsapp.list_chats(include_last_message=False)
        self.assertEqual(len(without), 2)
        self.assertTrue(all(chat.last_message is None for chat in without))

        with_last = whatsapp.list_chats(include_last_message=True)
        direct = next(chat for chat in with_last if chat.jid == "direct@lid")
        self.assertEqual(direct.last_message, "latest tied by id")
        self.assertEqual(whatsapp.get_chat("direct@lid", False).last_message, None)
        self.assertEqual(whatsapp.get_chat("direct@lid", True).last_message, "latest tied by id")

    def test_contact_chats_returns_each_chat_once(self):
        chats = whatsapp.get_contact_chats("contact@lid")
        self.assertEqual([chat.jid for chat in chats], ["direct@lid", "group@g.us"])
        self.assertEqual(chats[1].last_message, "group latest")

    def test_pagination_and_sort_are_validated(self):
        for kwargs in ({"limit": 0}, {"limit": 201}, {"page": -1}, {"sort_by": "sql"}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    whatsapp.list_chats(**kwargs)

    def test_messages_are_objects_instead_of_formatted_text(self):
        messages = whatsapp.list_messages(include_context=False)
        self.assertIsInstance(messages, list)
        self.assertIsInstance(messages[0], whatsapp.Message)
        contexts = whatsapp.list_messages(include_context=True)
        self.assertIsInstance(contexts[0], whatsapp.MessageContext)


class WhatsAppTransportTest(unittest.TestCase):
    class Response:
        status_code = 200
        text = "{}"

        @staticmethod
        def json():
            return {"success": True, "message": "ok", "message_id": "id"}

    def test_http_calls_have_a_timeout(self):
        with mock.patch.object(whatsapp.requests, "post", return_value=self.Response()) as post:
            success, _, _ = whatsapp.send_message("recipient", "hello")
        self.assertTrue(success)
        self.assertEqual(post.call_args.kwargs["timeout"], whatsapp.REQUEST_TIMEOUT)

    def test_generated_audio_is_removed_after_the_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.wav"
            generated = Path(tmp) / "generated.ogg"
            source.write_bytes(b"source")
            generated.write_bytes(b"generated")
            with (
                mock.patch.object(
                    whatsapp.audio, "convert_to_opus_ogg_temp", return_value=str(generated)
                ),
                mock.patch.object(whatsapp.requests, "post", return_value=self.Response()),
            ):
                success, _ = whatsapp.send_audio_message("recipient", str(source))
            self.assertTrue(success)
            self.assertFalse(generated.exists())

    def test_ffmpeg_timeout_becomes_a_readable_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.wav"
            source.write_bytes(b"source")
            with mock.patch.object(
                audio.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired("ffmpeg", audio.FFMPEG_TIMEOUT),
            ):
                with self.assertRaisesRegex(RuntimeError, "timed out"):
                    audio.convert_to_opus_ogg(str(source), str(Path(tmp) / "out.ogg"))


@unittest.skipIf(mcp_main is None, "MCP runtime is only installed in its project venv")
class MCPContractTest(unittest.TestCase):
    def test_list_messages_returns_json_ready_dictionaries(self):
        message = whatsapp.Message(
            timestamp=datetime(2026, 1, 2, 3, 4, 5),
            sender="sender",
            content="hello",
            is_from_me=False,
            chat_jid="chat@lid",
            id="id",
        )
        with mock.patch.object(mcp_main, "whatsapp_list_messages", return_value=[message]):
            result = mcp_main.list_messages(include_context=False)
        self.assertIsInstance(result, list)
        self.assertIsInstance(result[0], dict)
        self.assertEqual(result[0]["timestamp"], "2026-01-02T03:04:05")

        context = whatsapp.MessageContext(message=message, before=[], after=[])
        with mock.patch.object(mcp_main, "whatsapp_list_messages", return_value=[context]):
            result = mcp_main.list_messages(include_context=True)
        self.assertEqual(result[0]["message"]["id"], "id")
        self.assertEqual(result[0]["before"], [])


if __name__ == "__main__":
    unittest.main()
