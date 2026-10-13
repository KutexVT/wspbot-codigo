import importlib.util
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "bin/voz.py"
spec = importlib.util.spec_from_file_location("voz", SCRIPT)
voz = importlib.util.module_from_spec(spec)
spec.loader.exec_module(voz)


class VoiceTest(unittest.TestCase):
    def test_selection_respects_chat_length_links_probability_and_cooldown(self):
        config = voz.validar({"enabled": True, "jid": "test@lid", "probability": 1})
        text = "Oye no te preocupes que mañana te cuento"
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "messages.db"
            with closing(sqlite3.connect(db)) as conn, conn:
                conn.execute("CREATE TABLE messages (chat_jid, sender, media_type, timestamp)")
                conn.execute("INSERT INTO messages VALUES ('test@lid', '__tulpa__', 'audio', '2026-09-07 06:00:00-06:00')")
            now = 1788782400  # 2026-09-07 12:00 UTC
            self.assertFalse(voz.elegible(config, text, db, "test@lid", now + 1799))
            self.assertTrue(voz.elegible(config, text, db, "test@lid", now + 1800))
            for message, jid in [("Shi", "test@lid"), ("Oye mira https://example.org esto", "test@lid"), (text, "otro@lid")]:
                self.assertFalse(voz.elegible(config, message, db, jid, now + 4000))
            config["probability"] = 0
            self.assertFalse(voz.elegible(config, text, db, "test@lid", now + 4000))
            config.update(probability=1, enabled=False)
            self.assertFalse(voz.elegible(config, text, db, "test@lid", now + 4000))

    def test_spoken_abbreviations_keep_the_meaning(self):
        self.assertEqual(voz.hablado("Shi ntpp xq mañana voy x ti"), "sí no te preocupes porque mañana voy por ti")
        self.assertEqual(voz.hablado("Oye no te preocupes"), "Oye no te preocupes")

    def test_real_audio_check_rejects_missing_endings(self):
        text = "Oye no te preocupes que mañana te cuento bien"
        self.assertTrue(voz.contenido_coincide(text, "Oye, no te preocupes que mañana te cuento bien."))
        self.assertFalse(voz.contenido_coincide(text, "Oye no te preocupes que mañana te"))
        self.assertFalse(voz.contenido_coincide(text, "Oye no te preocupes que mañana te cuto mi toriendo"))
        self.assertFalse(voz.contenido_coincide(text, ""))

    def test_bad_configuration_is_rejected(self):
        for override in [{"probability": float("nan")}, {"enabled": "false"}, {"steps": 1.5}, {"timeout_seconds": 0}]:
            with self.subTest(override=override), self.assertRaises(ValueError):
                voz.validar({"enabled": True, "jid": "test@lid", **override})

    def test_timeout_removes_partial_audio_and_stays_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps({"enabled": True, "jid": "test@lid"}))
            args = [str(SCRIPT), "--modo", "voz", "--config", str(config), "--texto", "Oye"]
            with (mock.patch.object(sys, "argv", args),
                  mock.patch.dict(os.environ, {"WSP_JID": "test@lid", "WSP_MUDO": str(Path(tmp) / "MUDO")}),
                  mock.patch.object(voz.subprocess, "run", side_effect=subprocess.TimeoutExpired("tts", 90)) as run):
                with self.assertRaises(subprocess.TimeoutExpired):
                    voz.main()
            self.assertEqual(run.call_args.kwargs["env"]["HF_HUB_OFFLINE"], "1")
            self.assertEqual(list((Path(tmp) / "generados").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
