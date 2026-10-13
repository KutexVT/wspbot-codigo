import json
import os
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "bin/evento-wsp.py"
TARGET = "objetivo@lid"


class EventoWSPIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.state = self.base / "state"
        self.db = self.base / "messages.db"
        self.cursor = self.base / "cursor.txt"
        self.mudo = self.base / "MUDO"
        self.trace = self.base / "codex.trace"
        self.reg_trace = self.base / "registrar.trace"
        self.automation = self.base / "automation.toml"
        self.automations_dir = self.base / "automations"
        self.automations_dir.mkdir()
        self.fake_codex = self.base / "fake-codex.py"
        self.fake_pulso = self.base / "fake-pulso.py"
        self.fake_registrar = self.base / "fake-registrar.py"

        with sqlite3.connect(self.db) as conn:
            conn.execute(
                """CREATE TABLE messages (
                    id TEXT, chat_jid TEXT, content TEXT, timestamp TEXT,
                    is_from_me BOOLEAN, PRIMARY KEY (id, chat_jid)
                )"""
            )
        self.cursor.write_text(
            "LAST_GINGER_TS=2026-01-01 00:00:00\nLAST_VISTO_TS=2026-01-01 00:00:00\n",
            encoding="utf-8",
        )
        self.automation.write_text(
            'status = "PAUSED"\ntarget_thread_id = "thread-test"\n', encoding="utf-8"
        )
        self.fake_codex.write_text(
            """#!/usr/bin/env python3
import os, sqlite3, time
db, cursor, trace = os.environ['WSP_DB'], os.environ['WSP_CURSOR'], os.environ['TEST_TRACE']
with sqlite3.connect(db) as conn:
    row = conn.execute("select max(substr(timestamp,1,19)) from messages where chat_jid=? and is_from_me=0", (os.environ['WSP_JID'],)).fetchone()
snapshot = row[0]
with open(trace, 'a', encoding='utf-8') as f: f.write('START ' + str(snapshot) + '\\n')
time.sleep(float(os.environ.get('TEST_CODEX_SLEEP', '0.2')))
lines = open(cursor, encoding='utf-8').read().splitlines()
out = []
for line in lines:
    if line.startswith('LAST_VISTO_TS=') or line.startswith('LAST_GINGER_TS='):
        out.append(line.split('=', 1)[0] + '=' + snapshot)
    else:
        out.append(line)
open(cursor, 'w', encoding='utf-8').write('\\n'.join(out) + '\\n')
with open(trace, 'a', encoding='utf-8') as f: f.write('END ' + str(snapshot) + '\\n')
""",
            encoding="utf-8",
        )
        self.fake_pulso.write_text(
            """#!/usr/bin/env python3
import os
open(os.environ['WSP_TRASPASO'], 'w', encoding='utf-8').write('VISTO=2026-01-01 00:00:01\\nVISTO_GINGER=2026-01-01 00:00:01\\n')
print('ESTADO: NUEVOS')
print('MUDO: si — indefinido (solo /on lo levanta)')
""",
            encoding="utf-8",
        )
        self.fake_registrar.write_text(
            """#!/usr/bin/env python3
import os, sys
open(os.environ['TEST_REG_TRACE'], 'a', encoding='utf-8').write(' '.join(sys.argv[1:]) + '\\n')
""",
            encoding="utf-8",
        )
        for path in (self.fake_codex, self.fake_pulso, self.fake_registrar):
            path.chmod(0o755)

        self.env = os.environ.copy()
        self.env.update(
            {
                "WSP_BASE": str(self.base),
                "WSP_EVENT_STATE": str(self.state),
                "WSP_DB": str(self.db),
                "WSP_CURSOR": str(self.cursor),
                "WSP_MUDO": str(self.mudo),
                "WSP_TRASPASO": str(self.base / "traspaso"),
                "WSP_JID": TARGET,
                "WSP_CODEX_AUTOMATION": str(self.automation),
                "WSP_CODEX_AUTOMATIONS_DIR": str(self.automations_dir),
                "WSP_CODEX_BIN": str(self.fake_codex),
                "WSP_PULSO": str(self.fake_pulso),
                "WSP_REGISTRAR": str(self.fake_registrar),
                "WSP_EVENT_DEBOUNCE": "0.08",
                "WSP_EVENT_WATCH_INTERVAL": "0.05",
                "WSP_UMBRAL_SILENCIO": "999999",
                "TEST_TRACE": str(self.trace),
                "TEST_REG_TRACE": str(self.reg_trace),
                "TEST_CODEX_SLEEP": "0.25",
            }
        )

    def tearDown(self):
        self.tmp.cleanup()

    def insert(self, message_id, timestamp, chat=TARGET, from_me=0, content="hola"):
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "insert into messages values (?, ?, ?, ?, ?)",
                (message_id, chat, content, timestamp, from_me),
            )

    def notify(self, message_id="m1", chat=TARGET, from_me=False):
        cmd = [str(SCRIPT), "notify", "--chat-jid", chat, "--message-id", message_id]
        if from_me:
            cmd.append("--from-me")
        subprocess.run(cmd, env=self.env, check=True, timeout=5)

    def wait_for(self, predicate, timeout=6):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.03)
        self.fail("timeout esperando condicion")

    def state_json(self):
        try:
            return json.loads((self.state / "state.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def wait_idle(self):
        self.wait_for(
            lambda: bool(self.state_json())
            and not self.state_json().get("worker_active")
            and not self.state_json().get("pending_reasons")
        )

    def test_debounce_serializes_and_keeps_one_pending_turn(self):
        # Da margen suficiente para que las dos notificaciones de m4 entren mientras
        # el primer Codex falso sigue activo incluso en una maquina cargada.
        self.env["TEST_CODEX_SLEEP"] = "0.6"
        for n in range(1, 4):
            self.insert(f"m{n}", f"2026-01-01 00:00:0{n}")
            self.notify(f"m{n}")

        self.wait_for(lambda: self.trace.exists() and "START" in self.trace.read_text())
        self.insert("m4", "2026-01-01 00:00:04")
        self.notify("m4")
        self.notify("m4")
        self.wait_idle()

        lines = self.trace.read_text(encoding="utf-8").splitlines()
        self.assertEqual([line.split()[0] for line in lines], ["START", "END", "START", "END"])
        self.assertEqual(self.state_json()["runs"], 2)

    def test_wrong_chat_outgoing_and_empty_replay_do_not_run_codex(self):
        self.notify(chat="otro@lid")
        self.notify(from_me=True)
        time.sleep(0.15)
        self.assertFalse(self.trace.exists())

        self.insert("old", "2026-01-01 00:00:01")
        self.cursor.write_text(
            "LAST_GINGER_TS=2026-01-01 00:00:01\nLAST_VISTO_TS=2026-01-01 00:00:01\n",
            encoding="utf-8",
        )
        self.notify("old")
        self.wait_idle()
        self.assertFalse(self.trace.exists())
        self.assertEqual(self.state_json()["suppressed_empty"], 1)

    def test_mudo_batch_advances_locally_without_model_tokens(self):
        self.mudo.touch()
        self.insert("m1", "2026-01-01 00:00:01")
        self.notify("m1")
        self.wait_idle()

        self.assertFalse(self.trace.exists())
        args = self.reg_trace.read_text(encoding="utf-8")
        self.assertIn("--ts visto", args)
        self.assertIn("MUDO — 1 mensajes nuevos", args)
        self.assertEqual(self.state_json()["quiet_mute_batches"], 1)

    def test_watch_wakes_once_when_timed_mudo_expires(self):
        self.mudo.write_text("HASTA: 2020-01-01 00:00:00\n", encoding="utf-8")
        self.insert("old", "2026-01-01 00:00:01")
        self.cursor.write_text(
            "LAST_GINGER_TS=2026-01-01 00:00:01\nLAST_VISTO_TS=2026-01-01 00:00:01\n",
            encoding="utf-8",
        )
        watcher = subprocess.Popen([str(SCRIPT), "watch"], env=self.env)
        try:
            self.wait_for(lambda: self.trace.exists() and "END" in self.trace.read_text())
            time.sleep(0.2)
        finally:
            watcher.terminate()
            watcher.wait(timeout=3)
        self.assertEqual(self.trace.read_text().count("START"), 1)
        self.assertEqual(self.state_json()["runs"], 1)

    def test_watch_considers_silence_only_once_per_last_message(self):
        env = self.env.copy()
        env["WSP_UMBRAL_SILENCIO"] = "0"
        self.insert("old", "2026-01-01 00:00:01")
        self.cursor.write_text(
            "LAST_GINGER_TS=2026-01-01 00:00:01\nLAST_VISTO_TS=2026-01-01 00:00:01\n",
            encoding="utf-8",
        )
        watcher = subprocess.Popen([str(SCRIPT), "watch"], env=env)
        try:
            self.wait_for(lambda: self.trace.exists() and "END" in self.trace.read_text())
            time.sleep(0.2)
        finally:
            watcher.terminate()
            watcher.wait(timeout=3)
        self.assertEqual(self.trace.read_text().count("START"), 1)
        self.assertEqual(self.state_json()["runs"], 1)

    def write_automation(self, name, status, thread="thread-test"):
        folder = self.automations_dir / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "automation.toml").write_text(
            f'id = "{name}"\nstatus = "{status}"\ntarget_thread_id = "{thread}"\n',
            encoding="utf-8",
        )

    def test_another_active_automation_on_same_thread_blocks_the_run(self):
        """El scheduler de la app ignora worker.lock: si otra automation ACTIVE apunta al
        mismo hilo puede lanzar codex en paralelo y romper la vuelta. Mirar solo wspbot
        dejaba pasar wspbot-2."""
        self.write_automation("wspbot-2", "ACTIVE")
        self.insert("m1", "2026-01-01 00:00:02")
        self.notify("m1")
        self.wait_for(lambda: self.state_json().get("last_error"))
        self.assertFalse(self.trace.exists())
        self.assertEqual(self.state_json()["runs"], 0)
        self.assertIn("wspbot-2", self.state_json()["last_error"])
        # La vuelta no se pierde: queda pendiente para cuando se pause la automation
        self.assertIn("message", self.state_json()["pending_reasons"])

    def test_paused_automation_on_same_thread_does_not_block(self):
        self.write_automation("wspbot-2", "PAUSED")
        self.insert("m1", "2026-01-01 00:00:02")
        self.notify("m1")
        self.wait_for(lambda: self.trace.exists() and "END" in self.trace.read_text())
        self.assertEqual(self.state_json()["runs"], 1)

    def test_doctor_reports_active_automation_on_same_thread(self):
        self.write_automation("wspbot-2", "ACTIVE")
        proc = subprocess.run(
            [str(SCRIPT), "doctor"], env=self.env, text=True, capture_output=True
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("ACTIVE_EN_HILO=wspbot-2", proc.stdout)


if __name__ == "__main__":
    unittest.main()
