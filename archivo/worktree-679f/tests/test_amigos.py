import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "bin/wspbot"
RESOLVER = ROOT / "bin/persona-wsp"
BUSCAR = ROOT / "buscar.sh"


class AmigosLauncherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.bridge = self.base / "bridge"
        self.store = self.bridge / "store"
        self.store.mkdir(parents=True)
        (self.base / "nucleo").mkdir()
        (self.base / "memoria/amigos").mkdir(parents=True)
        (self.base / "PROMPT_AMIGOS.md").write_text("prompt amigos\n", encoding="utf-8")
        self.index = self.base / "personas.tsv"
        self.index.write_text(
            "ana\t111@lid\tthread-ana\nbea\t222@lid\tthread-bea\n",
            encoding="utf-8",
        )
        for safe in ("111_at_lid", "222_at_lid"):
            (self.base / "memoria/amigos" / safe).mkdir()

        self.db = self.store / "messages.db"
        with sqlite3.connect(self.db) as conn:
            conn.execute("create table messages (chat_jid text, timestamp text)")
            conn.executemany(
                "insert into messages values (?, ?)",
                [("111@lid", "2026-01-01 00:00:11"), ("222@lid", "2026-01-01 00:00:22")],
            )

        self.bridge_trace = self.base / "bridge.json"
        self.event_trace = self.base / "event.json"
        client = self.bridge / "whatsapp-client"
        client.write_text(
            """#!/usr/bin/env python3
import json, os
keys = [k for k in os.environ if k.startswith('WSP_')]
open(os.environ['TEST_BRIDGE_TRACE'], 'w', encoding='utf-8').write(
    json.dumps({k: os.environ[k] for k in sorted(keys)}))
""",
            encoding="utf-8",
        )
        client.chmod(0o755)

        self.event = self.base / "evento con espacio.py"
        self.event.write_text(
            """#!/usr/bin/env python3
import json, os, sys
open(os.environ['TEST_EVENT_TRACE'], 'w', encoding='utf-8').write(
    json.dumps({'argv': sys.argv[1:], 'env': {k: v for k, v in os.environ.items() if k.startswith('WSP_')}}))
print('AGENT=' + os.environ.get('WSP_AGENT', 'codex'))
print('JID=' + os.environ.get('WSP_JID', '000000000000000@lid'))
print('OK')
""",
            encoding="utf-8",
        )
        self.event.chmod(0o755)

        fake_bin = self.base / "fake-bin"
        fake_bin.mkdir()
        pgrep = fake_bin / "pgrep"
        pgrep.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        pgrep.chmod(0o755)

        self.env = {k: v for k, v in os.environ.items() if not k.startswith("WSP_")}
        self.env.update(
            {
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "WSP_BASE": str(self.base),
                "WSP_BRIDGE_DIR": str(self.bridge),
                "WSP_EVENTO": str(self.event),
                "WSP_PERSONA_RESOLVER": str(RESOLVER),
                "WSP_PERSONAS_INDEX": str(self.index),
                "WSP_GINGER_CODEX_THREAD_ID": "thread-ginger",
                "TEST_BRIDGE_TRACE": str(self.bridge_trace),
                "TEST_EVENT_TRACE": str(self.event_trace),
            }
        )

    def tearDown(self):
        self.tmp.cleanup()

    def run_launcher(self, *args):
        return subprocess.run(
            ["/bin/bash", str(LAUNCHER), *args],
            env=self.env,
            text=True,
            capture_output=True,
            timeout=5,
        )

    def bridge_env(self):
        return json.loads(self.bridge_trace.read_text(encoding="utf-8"))

    def test_without_flags_keeps_the_bridge_without_ai(self):
        proc = self.run_launcher()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.bridge_env()["WSP_EVENT_TRIGGER"], "")
        self.assertFalse(self.event_trace.exists())

    def test_without_chat_keeps_the_ginger_context(self):
        proc = self.run_launcher("--codex")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        env = self.bridge_env()
        self.assertNotIn("WSP_CODEX_PROMPT", env)
        self.assertNotIn("WSP_MEMORIA", env)
        self.assertNotIn("WSP_JID", env)
        self.assertEqual((self.base / "nucleo/eventos/agente").read_text().strip(), "codex")

    def test_chat_selects_the_friend_prompt_and_isolated_state(self):
        proc = self.run_launcher("--chat", "111@lid", "--codex")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        env = self.bridge_env()
        state = str(self.base / "nucleo/amigos/111_at_lid")
        self.assertEqual(env["WSP_JID"], "111@lid")
        self.assertIn("PROMPT_AMIGOS.md", env["WSP_CODEX_PROMPT"])
        self.assertEqual(env["WSP_MEMORIA"], str(self.base / "memoria/amigos/111_at_lid"))
        for key in ("WSP_CURSOR", "WSP_MUDO", "WSP_TRASPASO", "WSP_CITAS", "WSP_LOGS"):
            self.assertTrue(env[key].startswith(state), (key, env[key]))
        self.assertEqual(env["WSP_CODEX_THREAD_ID"], "thread-ana")
        self.assertEqual((Path(env["WSP_AGENT_FILE"])).read_text().strip(), "codex")

    def test_invalid_or_unregistered_jid_fails_before_effects(self):
        for jid in ("no-es-jid@lid", "999@lid"):
            self.bridge_trace.unlink(missing_ok=True)
            self.event_trace.unlink(missing_ok=True)
            proc = self.run_launcher("--chat", jid, "--claude")
            self.assertNotEqual(proc.returncode, 0)
            self.assertFalse(self.bridge_trace.exists())
            self.assertFalse(self.event_trace.exists())
        self.assertFalse((self.base / "nucleo/amigos").exists())

    def test_missing_memory_or_ginger_codex_thread_fails_before_bridge(self):
        (self.base / "memoria/amigos/111_at_lid").rmdir()
        missing = self.run_launcher("--chat", "111@lid", "--claude")
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("falta la memoria aislada", missing.stderr)
        self.assertFalse(self.bridge_trace.exists())

        (self.base / "memoria/amigos/111_at_lid").mkdir()
        self.index.write_text("ana\t111@lid\tthread-ginger\n", encoding="utf-8")
        reused = self.run_launcher("--chat", "111@lid", "--codex")
        self.assertNotEqual(reused.returncode, 0)
        self.assertIn("es el hilo de Contacto", reused.stderr)
        self.assertFalse(self.bridge_trace.exists())

    def test_two_friends_do_not_share_state_or_claude_session(self):
        states = []
        for jid in ("111@lid", "222@lid"):
            proc = self.run_launcher("--chat", jid, "--claude")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            env = self.bridge_env()
            states.append(
                tuple(
                    env[key]
                    for key in (
                        "WSP_CURSOR",
                        "WSP_MUDO",
                        "WSP_TRASPASO",
                        "WSP_CITAS",
                        "WSP_EVENT_STATE",
                        "WSP_LOGS",
                        "WSP_AGENT_FILE",
                        "WSP_CLAUDE_SESSION",
                    )
                )
            )
        self.assertTrue(set(states[0]).isdisjoint(states[1]))
        self.assertIn("2026-01-01 00:00:11", Path(states[0][0]).read_text())
        self.assertIn("2026-01-01 00:00:22", Path(states[1][0]).read_text())


class BuscarPersonaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        (self.base / "nucleo").mkdir()
        (self.base / "memoria/actual").mkdir(parents=True)
        (self.base / "memoria/amigos/111_at_lid").mkdir(parents=True)
        (self.base / "memoria/amigos/222_at_lid").mkdir(parents=True)
        (self.base / "memoria/amigos/333_at_lid").mkdir(parents=True)
        (self.base / "estilo").mkdir()
        (self.base / "nucleo/siempre.md").write_text("dato general\n", encoding="utf-8")
        (self.base / "memoria/actual/ginger.md").write_text("secreto ginger memoria\n", encoding="utf-8")
        (self.base / "memoria/amigos/111_at_lid/resumen.md").write_text(
            "secreto ana memoria\n", encoding="utf-8"
        )
        self.index = self.base / "personas.tsv"
        self.index.write_text(
            "ana\t111@lid\tthread-ana\nluna\t222@lid\t\nLUNA\t333@lid\t\n",
            encoding="utf-8",
        )
        self.db = self.base / "messages.db"
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "create table messages (id text, chat_jid text, content text, timestamp text, is_from_me integer, sender text)"
            )
            conn.executemany(
                "insert into messages values (?, ?, ?, ?, ?, ?)",
                [
                    ("g", "000000000000000@lid", "secreto ginger db", "2026-01-01 00:00:01", 0, ""),
                    ("a", "111@lid", "secreto ana db", "2026-01-01 00:00:02", 0, ""),
                    ("b", "222@lid", "secreto luna db", "2026-01-01 00:00:03", 0, ""),
                ],
            )
        self.env = os.environ.copy()
        self.env.update(
            {
                "WSP_BASE": str(self.base),
                "WSP_DB": str(self.db),
                "WSP_PERSONAS_INDEX": str(self.index),
                "WSP_PERSONA_RESOLVER": str(RESOLVER),
                "WSP_JID": "111@lid",
            }
        )

    def tearDown(self):
        self.tmp.cleanup()

    def buscar(self, *args):
        return subprocess.run(
            ["/bin/bash", str(BUSCAR), *args],
            env=self.env,
            text=True,
            capture_output=True,
            timeout=5,
        )

    def test_default_search_stays_on_ginger(self):
        proc = self.buscar("secreto")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("secreto ginger memoria", proc.stdout)
        self.assertIn("secreto ginger db", proc.stdout)
        self.assertNotIn("secreto ana", proc.stdout)

    def test_person_search_is_limited_to_one_friend(self):
        proc = self.buscar("--persona", "ana", "secreto")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("secreto ana memoria", proc.stdout)
        self.assertIn("secreto ana db", proc.stdout)
        self.assertNotIn("secreto ginger", proc.stdout)
        self.assertNotIn("secreto luna", proc.stdout)

    def test_friend_prompt_reuses_the_shared_sticker_catalog(self):
        prompt = (ROOT / "PROMPT_AMIGOS.md").read_text(encoding="utf-8")
        self.assertIn("media/stickers/INDICE.md", prompt)
        self.assertIn("estilo/user_messaging_style.md", prompt)
        self.assertIn("No copies el catálogo", prompt)

    def test_missing_or_ambiguous_alias_fails_clearly(self):
        missing = self.buscar("--persona", "nadie", "secreto")
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("alias inexistente", missing.stderr)
        ambiguous = self.buscar("--persona", "luna", "secreto")
        self.assertNotEqual(ambiguous.returncode, 0)
        self.assertIn("alias ambiguo", ambiguous.stderr)


if __name__ == "__main__":
    unittest.main()
