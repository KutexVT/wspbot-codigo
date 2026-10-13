import http.server
import json
import os
import subprocess
import tempfile
import threading
import unittest
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "bin/responder.sh"


class ResponderStyleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.db = self.base / "messages.db"
        self.db.touch()
        self.mudo = self.base / "MUDO"
        self.requests = []
        self.response_status = 200
        test = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                test.requests.append(body)
                response = b'{"success":true,"message_id":"test"}'
                self.send_response(test.response_status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.env = os.environ.copy()
        self.env.update(
            {
                "WSP_DB": str(self.db),
                "WSP_JID": "objetivo@lid",
                "WSP_MUDO": str(self.mudo),
                "WSP_VOZ_CONFIG": str(self.base / "voz.json"),
                "WSP_TRANSCRIPCIONES": str(self.base / "transcripciones"),
                "WSP_ENVIO_URL": "http://127.0.0.1:%d/api/send"
                % self.server.server_address[1],
            }
        )

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def run_responder(self, *args, env=None):
        return subprocess.run(
            [str(SCRIPT), *args],
            env=env or self.env,
            text=True,
            capture_output=True,
            timeout=5,
        )

    def test_clean_and_warning_messages_are_sent(self):
        clean = self.run_responder("Oye")
        warning = self.run_responder("hola")

        self.assertEqual(clean.returncode, 0, clean.stderr)
        self.assertEqual(warning.returncode, 0, warning.stderr)
        self.assertIn("W-MAYUS0", warning.stderr)
        self.assertEqual(len(self.requests), 2)

    def test_style_error_does_not_touch_the_bridge(self):
        result = self.run_responder("Hola.")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("estilo rechazo", result.stderr)
        self.assertIn("E-PUNTO-FINAL", result.stderr)
        self.assertEqual(self.requests, [])

    def test_linter_failure_does_not_touch_the_bridge(self):
        env = self.env.copy()
        env["WSP_ESTILO"] = "/dev/null"

        result = self.run_responder("Oye", env=env)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fallo el validador de estilo", result.stderr)
        self.assertEqual(self.requests, [])

    def test_dry_run_validates_without_sending(self):
        clean = self.run_responder("--dry", "Oye")
        rejected = self.run_responder("--dry", "Hola.")

        self.assertEqual(clean.returncode, 0, clean.stderr)
        self.assertIn("DRY", clean.stdout)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("E-PUNTO-FINAL", rejected.stderr)
        self.assertEqual(self.requests, [])

    def test_mudo_still_blocks_before_any_send(self):
        self.mudo.touch()

        result = self.run_responder("Oye")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("MUDO", result.stderr)
        self.assertEqual(self.requests, [])

    def fake_voice(self, action="audio"):
        """Simula solo el TTS; parser, estilo, MUDO, JSON y HTTP siguen siendo reales."""
        self.fake_audio = self.base / "nota.ogg"
        self.fake_audio.write_bytes(b"OggS")
        self.fake_audio.with_suffix(".txt").write_text("Oye no te preocupes\n")
        executable = self.base / "python3"
        executable.write_text(
            f"#!{sys.executable}\n"
            "import os,sys\nfrom pathlib import Path\n"
            "if sys.argv[1].endswith('/voz.py'):\n"
            f" Path({str(self.base / 'generado')!r}).touch()\n"
            + (f" Path({str(self.mudo)!r}).touch()\n" if action == "mudo" else "")
            + (" sys.exit(1)\n" if action == "error" else f" print({str(self.fake_audio)!r})\n")
            + "else:\n"
            f" os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])\n"
        )
        executable.chmod(0o700)
        self.env["PATH"] = str(self.base) + os.pathsep + self.env["PATH"]

    def test_audio_is_one_request_and_transcript_is_saved(self):
        self.fake_voice()
        result = self.run_responder("--voz", "Oye no te preocupes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("formato=audio", result.stdout)
        self.assertEqual(len(self.requests), 1)
        payload = json.loads(self.requests[0])
        self.assertEqual(payload["media_path"], str(self.fake_audio))
        self.assertEqual(payload["message"], "Oye no te preocupes")
        self.assertEqual((self.base / "transcripciones/test.txt").read_text().strip(), payload["message"])

    def test_synthesis_failure_falls_back_only_in_auto(self):
        self.fake_voice("error")
        automatic = self.run_responder("Oye no te preocupes")
        forced = self.run_responder("--voz", "Oye no te preocupes")
        self.assertEqual(automatic.returncode, 0, automatic.stderr)
        self.assertNotEqual(forced.returncode, 0)
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn("media_path", json.loads(self.requests[0]))

    def test_mudo_during_synthesis_blocks_send(self):
        self.fake_voice("mudo")
        result = self.run_responder("--voz", "Oye no te preocupes")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("MUDO", result.stderr)
        self.assertEqual(self.requests, [])

    def test_dry_and_text_do_not_generate(self):
        self.fake_voice()
        dry = self.run_responder("--dry", "--voz", "Oye")
        text = self.run_responder("--texto", "Oye")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertFalse((self.base / "generado").exists())
        self.assertEqual(len(self.requests), 1)

    def test_http_failure_never_sends_a_second_message(self):
        self.fake_voice()
        self.response_status = 500
        result = self.run_responder("Oye no te preocupes")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.requests), 1)
        self.assertFalse((self.base / "transcripciones/test.txt").exists())

    def test_missing_quote_argument_fails_without_looping(self):
        result = self.run_responder("--citar")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
