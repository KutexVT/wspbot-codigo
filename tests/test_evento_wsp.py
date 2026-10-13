import http.server
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import uuid
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
        self.codex_argv_trace = self.base / "codex-argv.trace"
        self.reg_trace = self.base / "registrar.trace"
        self.automation = self.base / "automation.toml"
        self.automations_dir = self.base / "automations"
        self.automations_dir.mkdir()
        self.notify_trace = self.base / "notify.trace"
        self.prompt_trace = self.base / "prompt.trace"
        self.pulso_trace = self.base / "pulso.trace"
        self.fake_notify = self.base / "fake-notify.py"
        self.fake_claude = self.base / "fake-claude.py"
        self.argv_trace = self.base / "argv.trace"
        self.locks_dir = self.base / "thread-writer-locks"
        self.locks_dir.mkdir()
        self.start_health_server()
        self.fake_codex = self.base / "fake-codex.py"
        self.fake_pulso = self.base / "fake-pulso.py"
        self.fake_registrar = self.base / "fake-registrar.py"

        conn = sqlite3.connect(self.db)
        with conn:
            conn.execute(
                """CREATE TABLE messages (
                    id TEXT, chat_jid TEXT, content TEXT, timestamp TEXT,
                    is_from_me BOOLEAN, sender TEXT, media_type TEXT,
                    PRIMARY KEY (id, chat_jid)
                )"""
            )
        conn.close()
        self.cursor.write_text(
            "LAST_GINGER_TS=2026-01-01 00:00:00\nLAST_VISTO_TS=2026-01-01 00:00:00\n",
            encoding="utf-8",
        )
        self.automation.write_text(
            'status = "PAUSED"\ntarget_thread_id = "thread-test"\n', encoding="utf-8"
        )
        self.fake_codex.write_text(
            """#!/usr/bin/env python3
import json, os, sqlite3, sys, time
db, cursor, trace = os.environ['WSP_DB'], os.environ['WSP_CURSOR'], os.environ['TEST_TRACE']
with sqlite3.connect(db) as conn:
    row = conn.execute("select max(substr(timestamp,1,19)) from messages where chat_jid=? and coalesce(sender, '') <> '__tulpa__'", (os.environ['WSP_JID'],)).fetchone()
snapshot = row[0]
open(os.environ['TEST_PROMPT_TRACE'], 'a', encoding='utf-8').write(str(sys.argv[-1]) + '\\n')
open(os.environ['TEST_CODEX_ARGV_TRACE'], 'a', encoding='utf-8').write(json.dumps(sys.argv[1:]) + '\\n')
with open(trace, 'a', encoding='utf-8') as f: f.write('START ' + str(snapshot) + '\\n')
time.sleep(float(os.environ.get('TEST_CODEX_SLEEP', '0.2')))
lines = open(cursor, encoding='utf-8').read().splitlines()
out = []
for line in lines:
    if snapshot and (line.startswith('LAST_VISTO_TS=') or line.startswith('LAST_GINGER_TS=')):
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
import os, sys
open(os.environ['TEST_PULSO_TRACE'], 'a', encoding='utf-8').write('RUN\\n')
if os.environ.get('TEST_PULSO_FAIL'):
    print('fallo de pulso de prueba', file=sys.stderr)
    raise SystemExit(7)
ginger = os.environ.get('TEST_VISTO_GINGER', '2026-01-01 00:00:01')
open(os.environ['WSP_TRASPASO'], 'w', encoding='utf-8').write(f'VISTO=2026-01-01 00:00:01\\nVISTO_GINGER={ginger}\\n')
print('ESTADO: NUEVOS')
mudo = os.environ.get('TEST_MUDO_LINE', 'MUDO: si — indefinido (solo /on lo levanta)')
if 'acaba de expirar' in mudo or mudo == 'MUDO: no':
    try: os.unlink(os.environ['WSP_MUDO'])
    except FileNotFoundError: pass
print(mudo)
""",
            encoding="utf-8",
        )
        self.fake_claude.write_text(
            """#!/usr/bin/env python3
import json, os, sys
record = {'argv': sys.argv[1:], 'stdin': sys.stdin.read()}
open(os.environ['TEST_ARGV_TRACE'], 'a', encoding='utf-8').write(json.dumps(record) + '\\n')
""",
            encoding="utf-8",
        )
        self.fake_busy_codex = self.base / "fake-busy-codex.py"
        self.fake_busy_codex.write_text(
            """#!/usr/bin/env python3
import sys
print('Error: thread/resume: thread/resume failed: thread thread-test '
      'already has an active writer (code -32600)')
sys.exit(1)
""",
            encoding="utf-8",
        )
        self.fake_notify.write_text(
            """#!/usr/bin/env python3
import os, sys
open(os.environ['TEST_NOTIFY_TRACE'], 'a', encoding='utf-8').write(' | '.join(sys.argv[1:]) + '\\n')
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
        for path in (self.fake_codex, self.fake_pulso, self.fake_registrar, self.fake_notify, self.fake_claude, self.fake_busy_codex):
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
                "TEST_NOTIFY_TRACE": str(self.notify_trace),
                "TEST_PROMPT_TRACE": str(self.prompt_trace),
                "TEST_CODEX_ARGV_TRACE": str(self.codex_argv_trace),
                "TEST_PULSO_TRACE": str(self.pulso_trace),
                "WSP_CODEX_PROMPT": "vuelta-normal",
                "WSP_SALUD_URL": self.health_url,
                "WSP_NOTIFY_SEND": str(self.fake_notify),
                "WSP_MARCA_AVISO": str(self.base / "aviso_bridge"),
                "WSP_EVENT_HEALTH_INTERVAL": "0.05",
                "WSP_AVISO_CADA_MIN": "0",
                "WSP_EVENT_GRACIA": "0",
                "WSP_CLAUDE_BIN": str(self.fake_claude),
                "TEST_ARGV_TRACE": str(self.argv_trace),
                "WSP_AGENT_FILE": str(self.base / "agente"),
                "WSP_CODEX_LOCKS": str(self.locks_dir),
                "WSP_EVENT_REINTENTO": "0.1",
                "WSP_COMPACT_CADA_MIN": "0",
            }
        )

    def start_health_server(self):
        """El bridge real corre en 127.0.0.1:8080. Sin este servidor de mentira los tests
        leerian su salud y pasarian o fallarian segun estuviera encendido."""
        test = self
        self.health_alive = True

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = (
                    b'{"connected":true,"logged_in":true}'
                    if test.health_alive
                    else b'{"connected":false,"logged_in":true}'
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.health_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.health_server.serve_forever, daemon=True).start()
        self.health_url = "http://127.0.0.1:%d/api/health" % self.health_server.server_address[1]

    def tearDown(self):
        self.health_server.shutdown()
        self.health_server.server_close()
        self.tmp.cleanup()

    def insert(
        self, message_id, timestamp, chat=TARGET, from_me=0, content="hola", sender="",
        media_type=""
    ):
        conn = sqlite3.connect(self.db)
        with conn:
            conn.execute(
                "insert into messages values (?, ?, ?, ?, ?, ?, ?)",
                (message_id, chat, content, timestamp, from_me, sender, media_type),
            )
        conn.close()

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

    def test_wrong_chat_and_empty_replays_do_not_run_codex(self):
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
        # El notify propio ya se acepta para los mensajes de Mikel. Sin fila en la base
        # se descarta como vacio igual que el replay viejo de abajo.
        self.assertEqual(self.state_json()["suppressed_empty"], 2)

    def test_mikels_phone_wakes_the_worker_but_the_tulpa_does_not(self):
        self.insert("mikel", "2026-01-01 00:00:01", from_me=1, sender="mikel")
        self.notify("mikel", from_me=True)
        self.wait_idle()
        self.assertEqual(self.state_json()["runs"], 1)

        self.insert("tulpa", "2026-01-01 00:00:02", from_me=1, sender="__tulpa__")
        self.notify("tulpa", from_me=True)
        self.wait_idle()
        self.assertEqual(self.state_json()["runs"], 1)

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

    def test_mudo_batch_without_ginger_omits_responded_cursor(self):
        self.env["TEST_VISTO_GINGER"] = ""
        self.mudo.touch()
        self.insert(
            "mikel", "2026-01-01 00:00:01", from_me=1, sender="mikel"
        )
        self.notify("mikel", from_me=True)
        self.wait_idle()

        self.assertFalse(self.trace.exists())
        args = self.reg_trace.read_text(encoding="utf-8")
        self.assertNotIn("--ts", args)
        self.assertIn("MUDO — 1 mensajes nuevos", args)
        self.assertEqual(self.state_json()["last_error"], "")
        self.assertEqual(self.state_json()["quiet_mute_batches"], 1)

    def test_sticker_joins_the_batch_without_restarting_its_debounce(self):
        self.insert("sticker", "2026-01-01 00:00:02", media_type="sticker")
        self.insert("text", "2026-01-01 00:00:03")
        with unittest.mock.patch.dict(os.environ, self.env, clear=False):
            modulo = self.cargar_modulo(self.base / "agente-sticker")

        sticker = unittest.mock.Mock(chat_jid=TARGET, message_id="sticker")
        self.seed_state(worker_active=True, worker_pid=os.getpid())
        self.assertEqual(modulo.notify(sticker), 0)
        self.assertGreater(self.state_json()["last_event_at"], 0)

        self.seed_state(
            pending_reasons=["message"], last_event_at=123.0,
            worker_active=True, worker_pid=os.getpid()
        )
        self.assertEqual(modulo.notify(sticker), 0)
        self.assertEqual(self.state_json()["last_event_at"], 123.0)
        self.assertEqual(self.state_json()["last_message_id"], "sticker")

        text = unittest.mock.Mock(chat_jid=TARGET, message_id="text")
        self.assertEqual(modulo.notify(text), 0)
        self.assertGreater(self.state_json()["last_event_at"], 123.0)
        self.assertFalse(self.trace.exists())

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

    def claude_runs(self):
        if not self.argv_trace.exists():
            return []
        runs = []
        for line in self.argv_trace.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            runs.append(record["argv"] + ([record["stdin"]] if record["stdin"] else []))
        return runs

    def claude_raw_runs(self):
        if not self.argv_trace.exists():
            return []
        return [
            json.loads(line)
            for line in self.argv_trace.read_text(encoding="utf-8").splitlines()
        ]

    def prompts(self):
        if not self.prompt_trace.exists():
            return []
        return self.prompt_trace.read_text(encoding="utf-8").splitlines()

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
        # Por runs y no por el "END" del trace: el fake escribe END y el worker actualiza
        # el contador despues, asi que esperar por END deja una carrera abierta.
        self.wait_for(lambda: self.state_json().get("runs"))
        self.assertEqual(self.state_json()["runs"], 1)

    def test_doctor_reports_active_automation_on_same_thread(self):
        self.write_automation("wspbot-2", "ACTIVE")
        proc = subprocess.run(
            [str(SCRIPT), "doctor"], env=self.env, text=True, capture_output=True
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("ACTIVE_EN_HILO=wspbot-2", proc.stdout)

    def test_watcher_warns_when_the_bridge_is_down(self):
        """Sin heartbeat el aviso de bridge caido no lo manda nadie mas: pulso.sh solo
        corre dentro de una vuelta y un bridge muerto no dispara ninguna."""
        self.health_alive = False
        watcher = subprocess.Popen([str(SCRIPT), "watch"], env=self.env)
        try:
            self.wait_for(lambda: self.notify_trace.exists())
            time.sleep(0.2)
        finally:
            watcher.terminate()
            watcher.wait(timeout=3)
        aviso = self.notify_trace.read_text()
        self.assertIn("critical", aviso)
        self.assertIn("WSP Bot — bridge caido", aviso)
        self.assertIn("desconectado de WhatsApp", aviso)
        self.assertTrue(self.state_json()["bridge_down_since"])

    def test_bridge_down_does_not_wake_the_model_on_silence(self):
        """La base esta congelada: el silencio que se lea ahi no es silencio real."""
        env = self.env.copy()
        env["WSP_UMBRAL_SILENCIO"] = "0"
        self.health_alive = False
        self.insert("old", "2026-01-01 00:00:01")
        watcher = subprocess.Popen([str(SCRIPT), "watch"], env=env)
        try:
            self.wait_for(lambda: self.notify_trace.exists())
            time.sleep(0.4)
        finally:
            watcher.terminate()
            watcher.wait(timeout=3)
        self.assertFalse(self.trace.exists())
        self.assertEqual(self.state_json()["runs"], 0)

    def test_a_bridge_still_starting_up_does_not_trigger_a_false_alarm(self):
        """El bridge lanza el watcher ANTES de levantar el HTTP, asi que la primera
        consulta siempre falla. Sin gracia cada `wspbot` disparaba un aviso de mentira."""
        env = self.env.copy()
        env["WSP_EVENT_GRACIA"] = "30"
        self.health_alive = False
        watcher = subprocess.Popen([str(SCRIPT), "watch"], env=env)
        try:
            self.wait_for(lambda: self.state_json().get("bridge_down_since"))
            self.health_alive = True
            self.wait_for(lambda: not self.state_json().get("bridge_down_since"))
        finally:
            watcher.terminate()
            watcher.wait(timeout=3)
        self.assertFalse(self.notify_trace.exists(), "aviso disparado durante la gracia")

    def test_bridge_starting_replaces_a_persisted_outage_with_a_fresh_grace(self):
        """Una caida guardada antes del reinicio no puede avisar apenas abre wspbot."""
        old = time.time() - 3600
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "state.json").write_text(
            json.dumps({"bridge_down_since": old}), encoding="utf-8"
        )
        marker = Path(self.env["WSP_MARCA_AVISO"])
        marker.touch()

        before = time.time()
        proc = subprocess.run(
            [str(SCRIPT), "bridge-starting"],
            env=self.env,
            text=True,
            capture_output=True,
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("BRIDGE=STARTING", proc.stdout)
        self.assertGreaterEqual(self.state_json()["bridge_down_since"], before)
        self.assertFalse(marker.exists())

    def test_real_bridge_failure_warns_after_the_new_startup_grace(self):
        env = self.env.copy()
        env["WSP_EVENT_GRACIA"] = "0.2"
        self.health_alive = False
        subprocess.run([str(SCRIPT), "bridge-starting"], env=env, check=True)
        watcher = subprocess.Popen([str(SCRIPT), "watch"], env=env)
        try:
            time.sleep(0.1)
            self.assertFalse(self.notify_trace.exists())
            self.wait_for(lambda: self.notify_trace.exists())
        finally:
            watcher.terminate()
            watcher.wait(timeout=3)
        self.assertIn("WSP Bot — bridge caido", self.notify_trace.read_text())

    def test_bridge_recovery_clears_the_down_marker(self):
        self.health_alive = False
        watcher = subprocess.Popen([str(SCRIPT), "watch"], env=self.env)
        try:
            self.wait_for(lambda: self.state_json().get("bridge_down_since"))
            self.health_alive = True
            self.wait_for(lambda: not self.state_json().get("bridge_down_since"))
        finally:
            watcher.terminate()
            watcher.wait(timeout=3)
        self.assertEqual(self.state_json()["bridge_down_since"], 0.0)

    def test_first_compaction_does_not_fire_on_a_fresh_state(self):
        """El reloj arranca la primera vez que se mira. Un hilo recien creado no tiene
        nada que recortar y compactar de entrada seria gasto puro."""
        env = self.env.copy()
        env["WSP_COMPACT_CADA_MIN"] = "60"
        watcher = subprocess.Popen([str(SCRIPT), "watch"], env=env)
        try:
            self.wait_for(lambda: self.state_json().get("last_compact_at"))
            time.sleep(0.3)
        finally:
            watcher.terminate()
            watcher.wait(timeout=3)
        self.assertFalse(self.trace.exists())
        self.assertEqual(self.state_json()["compactions"], 0)

    def test_watcher_queues_the_compaction_when_the_clock_runs_out(self):
        env = self.env.copy()
        env["WSP_COMPACT_CADA_MIN"] = "0.0001"
        watcher = subprocess.Popen([str(SCRIPT), "watch"], env=env)
        try:
            self.wait_for(lambda: self.state_json().get("compactions"))
            time.sleep(0.2)
        finally:
            watcher.terminate()
            watcher.wait(timeout=3)
        self.assertIn("/compactar", self.prompts())
        # Compactar no es una vuelta del bot: no cuenta como run ni escribe al chat
        self.assertEqual(self.state_json()["runs"], 0)

    def seed_state(self, **fields):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "state.json").write_text(json.dumps(fields), encoding="utf-8")

    def run_worker(self, env=None):
        return subprocess.run(
            [str(SCRIPT), "worker"], env=env or self.env, text=True, capture_output=True
        )

    def test_compaction_runs_after_the_turn_not_before(self):
        """Primero se le contesta a Contacto y solo despues se recorta el hilo.

        Se arranca el worker a mano con las dos razones ya en la cola: dejarselo al
        watcher haria que la carrera decidiera el orden y el test no probaria nada."""
        self.insert("m1", "2026-01-01 00:00:02")
        self.seed_state(pending_reasons=["compact", "message"], last_event_at=0.0)
        self.assertEqual(self.run_worker().returncode, 0)
        self.assertEqual(self.prompts(), [self.env["WSP_CODEX_PROMPT"], "/compactar"])
        self.assertEqual(self.state_json()["runs"], 1)
        self.assertEqual(self.state_json()["compactions"], 1)

    def test_compaction_alone_does_not_count_as_an_empty_check(self):
        """Sin mensajes la vuelta seria un SKIP vacio. Compactar no lo es: hay trabajo."""
        self.seed_state(pending_reasons=["compact"], last_compact_at=1.0)
        self.assertEqual(self.run_worker().returncode, 0)
        self.assertEqual(self.prompts(), ["/compactar"])
        self.assertEqual(self.state_json()["suppressed_empty"], 0)
        self.assertEqual(self.state_json()["runs"], 0)

    def test_a_failed_compaction_is_requeued(self):
        env = self.env.copy()
        env["WSP_CODEX_BIN"] = "/bin/false"
        self.seed_state(pending_reasons=["compact"], last_compact_at=1.0)
        self.assertEqual(self.run_worker(env).returncode, 1)
        self.assertIn("compact", self.state_json()["pending_reasons"])
        self.assertEqual(self.state_json()["compactions"], 0)

    def test_claude_fixes_the_session_on_the_first_turn_and_resumes_after(self):
        """El uuid se fija una vez y se reanuda siempre. Si cada vuelta abriera una sesion
        nueva el bot no recordaria nada de la anterior."""
        env = self.env.copy()
        env["WSP_AGENT"] = "claude"
        self.insert("m1", "2026-01-01 00:00:02")
        self.seed_state(pending_reasons=["message"], last_event_at=0.0)
        self.assertEqual(self.run_worker(env).returncode, 0)

        session_file = self.state / "claude-session"
        session = session_file.read_text(encoding="utf-8").strip()
        uuid.UUID(session)  # revienta si no es un uuid valido
        primera = self.claude_runs()[0]
        self.assertIn("-p", primera)
        self.assertIn("--dangerously-skip-permissions", primera)
        self.assertEqual(primera[primera.index("--session-id") + 1], session)
        self.assertNotIn("--resume", primera)

        # Segunda vuelta: mismo uuid, pero reanudando
        self.insert("m2", "2026-01-01 00:00:03")
        self.seed_state(pending_reasons=["message"], last_event_at=0.0)
        self.assertEqual(self.run_worker(env).returncode, 0)
        segunda = self.claude_runs()[1]
        self.assertEqual(segunda[segunda.index("--resume") + 1], session)
        self.assertNotIn("--session-id", segunda)
        self.assertEqual(session_file.read_text(encoding="utf-8").strip(), session)

    def test_claude_receives_one_precomputed_pulso(self):
        env = self.env.copy()
        env["WSP_AGENT"] = "claude"
        self.insert("m1", "2026-01-01 00:00:02")
        self.seed_state(pending_reasons=["message"], last_event_at=0.0)

        self.assertEqual(self.run_worker(env).returncode, 0)

        self.assertEqual(self.pulso_trace.read_text(encoding="utf-8"), "RUN\n")
        prompt = self.claude_runs()[0][-1]
        self.assertIn(self.env["WSP_CODEX_PROMPT"], prompt)
        self.assertIn("--- SALIDA_PULSO_PREVIA ---", prompt)
        self.assertIn("ESTADO: NUEVOS", prompt)
        self.assertIn("NO vuelvas a correr pulso.sh", prompt)
        self.assertNotIn("SALIDA_PULSO_PREVIA", " ".join(self.claude_raw_runs()[0]["argv"]))
        self.assertIn("SALIDA_PULSO_PREVIA", self.claude_raw_runs()[0]["stdin"])

    def test_codex_keeps_prompt_and_gets_network_for_the_local_bridge(self):
        self.insert("m1", "2026-01-01 00:00:02")
        self.seed_state(pending_reasons=["message"], last_event_at=0.0)

        self.assertEqual(self.run_worker().returncode, 0)

        self.assertFalse(self.pulso_trace.exists())
        self.assertEqual(self.prompts(), [self.env["WSP_CODEX_PROMPT"]])
        argv = json.loads(self.codex_argv_trace.read_text(encoding="utf-8"))
        self.assertIn("--approve-for-me", argv)
        self.assertIn("sandbox_workspace_write.network_access=true", argv)

    def test_failed_precomputed_pulso_does_not_start_claude_and_requeues(self):
        env = self.env.copy()
        env["WSP_AGENT"] = "claude"
        env["TEST_PULSO_FAIL"] = "1"
        self.insert("m1", "2026-01-01 00:00:02")
        self.seed_state(pending_reasons=["message"], last_event_at=0.0)

        self.assertEqual(self.run_worker(env).returncode, 1)

        self.assertEqual(self.claude_runs(), [])
        self.assertIn("message", self.state_json()["pending_reasons"])
        self.assertIn("pulso antes de Claude termino con 7", self.state_json()["last_error"])

    def test_mudo_expiry_reuses_its_pulso_for_claude(self):
        env = self.env.copy()
        env["WSP_AGENT"] = "claude"
        env["TEST_MUDO_LINE"] = "MUDO: acaba de expirar"
        self.mudo.touch()
        self.insert("m1", "2026-01-01 00:00:02")
        self.seed_state(pending_reasons=["message"], last_event_at=0.0)

        self.assertEqual(self.run_worker(env).returncode, 0)

        self.assertEqual(self.pulso_trace.read_text(encoding="utf-8"), "RUN\n")
        self.assertIn("MUDO: acaba de expirar", self.claude_runs()[0][-1])
        self.assertFalse(self.mudo.exists())

    def test_claude_passes_the_model_when_asked(self):
        env = self.env.copy()
        env["WSP_AGENT"] = "claude"
        env["WSP_CLAUDE_MODEL"] = "opus"
        self.insert("m1", "2026-01-01 00:00:02")
        self.seed_state(pending_reasons=["message"], last_event_at=0.0)
        self.assertEqual(self.run_worker(env).returncode, 0)
        run = self.claude_runs()[0]
        self.assertEqual(run[run.index("--model") + 1], "opus")

    def test_claude_reads_model_and_effort_from_disk_with_environment_priority(self):
        env = self.env.copy()
        env["WSP_AGENT"] = "claude"
        (self.state / "modelo").parent.mkdir(parents=True, exist_ok=True)
        (self.state / "modelo").write_text("sonnet\n", encoding="utf-8")
        (self.state / "effort").write_text("high\n", encoding="utf-8")
        self.insert("m1", "2026-01-01 00:00:02")
        self.seed_state(pending_reasons=["message"], last_event_at=0.0)
        self.assertEqual(self.run_worker(env).returncode, 0)
        run = self.claude_runs()[0]
        self.assertEqual(run[run.index("--model") + 1], "sonnet")
        self.assertEqual(run[run.index("--effort") + 1], "high")

        env["WSP_CLAUDE_MODEL"] = "opus"
        env["WSP_CLAUDE_EFFORT"] = "xhigh"
        self.insert("m2", "2026-01-01 00:00:03")
        self.seed_state(pending_reasons=["message"], last_event_at=0.0)
        self.assertEqual(self.run_worker(env).returncode, 0)
        run = self.claude_runs()[1]
        self.assertEqual(run[run.index("--model") + 1], "opus")
        self.assertEqual(run[run.index("--effort") + 1], "xhigh")

    def test_invalid_claude_effort_is_ignored(self):
        env = self.env.copy()
        env["WSP_AGENT"] = "claude"
        env["WSP_CLAUDE_EFFORT"] = "turbo"
        self.insert("m1", "2026-01-01 00:00:02")
        self.seed_state(pending_reasons=["message"], last_event_at=0.0)
        self.assertEqual(self.run_worker(env).returncode, 0)
        self.assertNotIn("--effort", self.claude_runs()[0])

    def test_claude_never_touches_the_codex_automations(self):
        """Con Claude no hay hilo de Codex, asi que una automation ACTIVE no bloquea."""
        env = self.env.copy()
        env["WSP_AGENT"] = "claude"
        self.write_automation("wspbot-2", "ACTIVE")
        self.insert("m1", "2026-01-01 00:00:02")
        self.seed_state(pending_reasons=["message"], last_event_at=0.0)
        self.assertEqual(self.run_worker(env).returncode, 0)
        self.assertEqual(len(self.claude_runs()), 1)
        self.assertEqual(self.state_json()["runs"], 1)

    def test_an_unknown_agent_fails_loudly(self):
        env = self.env.copy()
        env["WSP_AGENT"] = "gemini"
        self.insert("m1", "2026-01-01 00:00:02")
        self.seed_state(pending_reasons=["message"], last_event_at=0.0)
        self.assertEqual(self.run_worker(env).returncode, 1)
        self.assertIn("no reconocido", self.state_json()["last_error"])
        # La vuelta no se pierde por una variable mal escrita
        self.assertIn("message", self.state_json()["pending_reasons"])

    def test_bad_configuration_stays_paused_until_something_changes(self):
        env = self.env.copy()
        env["WSP_AGENT"] = "gemini"
        self.insert("m1", "2026-01-01 00:00:02")
        self.seed_state(pending_reasons=["message"], last_event_at=0.0)
        self.assertEqual(self.run_worker(env).returncode, 1)
        self.assertEqual(self.state_json()["retry_class"], "config")
        self.assertIn("vuelta pausada", self.notify_trace.read_text(encoding="utf-8"))

        watcher = subprocess.Popen([str(SCRIPT), "watch"], env=env)
        try:
            time.sleep(0.4)
        finally:
            watcher.terminate()
            watcher.wait(timeout=3)
        self.assertEqual(self.state_json()["retries"], 0)

    def test_usage_limit_is_classified_for_exponential_backoff(self):
        quota = self.base / "fake-quota.py"
        quota.write_text(
            "#!/usr/bin/env python3\n"
            "print(\"ERROR: You've hit your usage limit\")\n"
            "raise SystemExit(1)\n",
            encoding="utf-8",
        )
        quota.chmod(0o755)
        env = self.env.copy()
        env["WSP_CODEX_BIN"] = str(quota)
        self.insert("m1", "2026-01-01 00:00:02")
        self.seed_state(pending_reasons=["message"], last_event_at=0.0)
        self.assertEqual(self.run_worker(env).returncode, 1)
        state = self.state_json()
        self.assertEqual(state["retry_class"], "quota")
        self.assertIn("cuota agotada", state["last_error"])

    def test_doctor_checks_the_engine_that_is_actually_selected(self):
        env = self.env.copy()
        env["WSP_AGENT"] = "claude"
        proc = subprocess.run(
            [str(SCRIPT), "doctor"], env=env, text=True, capture_output=True
        )
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("AGENT=claude", proc.stdout)
        # Con Claude no se mira ningun hilo de Codex
        self.assertNotIn("THREAD=", proc.stdout)
        self.assertIn("COMPACTAR=no (automatico)", proc.stdout)

    def write_agent_file(self, value):
        (self.base / "agente").write_text(value + "\n", encoding="utf-8")

    def doctor(self, env=None):
        return subprocess.run(
            [str(SCRIPT), "doctor"], env=env or self.env, text=True, capture_output=True
        )

    def test_the_saved_engine_beats_the_default(self):
        """Sin esto `doctor` mentia: leia solo el entorno, asi que una terminal sin el
        export decia codex mientras el bridge corria con Claude."""
        self.write_agent_file("claude")
        env = self.env.copy()
        env.pop("WSP_AGENT", None)
        self.assertIn("AGENT=claude", self.doctor(env).stdout)

    def test_the_environment_beats_the_saved_engine(self):
        self.write_agent_file("claude")
        env = self.env.copy()
        env["WSP_AGENT"] = "codex"
        self.assertIn("AGENT=codex", self.doctor(env).stdout)

    def test_a_corrupt_engine_file_falls_back_instead_of_breaking(self):
        self.write_agent_file("gemini-o-lo-que-sea")
        env = self.env.copy()
        env.pop("WSP_AGENT", None)
        proc = self.doctor(env)
        self.assertIn("AGENT=codex", proc.stdout)
        self.assertEqual(proc.returncode, 0, proc.stdout)

    def test_a_busy_codex_thread_gets_a_readable_error(self):
        """La app de ChatGPT abre thread-writer-locks/<uuid>.lock mientras tiene el hilo
        cargado, y con eso puesto `codex exec resume` falla siempre con un error criptico.
        Se traduce a algo accionable y se nombra a quien lo tiene."""
        lock = self.locks_dir / "thread-test.lock"
        lock.touch()
        # La referencia al file object es obligatoria: sin ella CPython lo cierra al
        # instante y el lock nunca llega a estar tomado.
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                f"import time, sys\nf = open({str(lock)!r}, 'a')\n"
                "sys.stdout.write('listo'); sys.stdout.flush()\ntime.sleep(30)",
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        env = self.env.copy()
        env["WSP_CODEX_BIN"] = str(self.fake_busy_codex)
        try:
            self.assertEqual(holder.stdout.read(5), "listo")
            self.insert("m1", "2026-01-01 00:00:02")
            self.seed_state(pending_reasons=["message"], last_event_at=0.0)
            self.assertEqual(self.run_worker(env).returncode, 1)
            error = self.state_json()["last_error"]
            self.assertIn(str(holder.pid), error)
            self.assertIn("WSP_AGENT=claude", error)
            self.assertIn("message", self.state_json()["pending_reasons"])
        finally:
            holder.stdout.close()
            holder.terminate()
            holder.wait(timeout=5)

    def test_the_writer_scan_is_fast_enough_for_doctor(self):
        """Mira /proc entero. Si tardara segundos no podria vivir ni en doctor."""
        (self.locks_dir / "thread-test.lock").touch()
        inicio = time.monotonic()
        self.doctor()
        self.assertLess(time.monotonic() - inicio, 3.0)

    def test_the_watcher_rescues_a_turn_left_behind_by_a_failure(self):
        """Una vuelta que falla se reencola pero el worker sale. Sin rescate se quedaba
        esperando el siguiente mensaje de Contacto, o sea mudo indefinidamente."""
        self.insert("m1", "2026-01-01 00:00:02")
        self.seed_state(pending_reasons=["message"], last_event_at=0.0, worker_active=False)
        watcher = subprocess.Popen([str(SCRIPT), "watch"], env=self.env)
        try:
            self.wait_for(lambda: self.state_json().get("runs"))
        finally:
            watcher.terminate()
            watcher.wait(timeout=3)
        self.assertEqual(self.state_json()["runs"], 1)
        self.assertGreaterEqual(self.state_json()["retries"], 1)
        self.assertEqual(self.state_json()["pending_reasons"], [])

    def test_unexpected_database_error_requeues_and_releases_the_worker(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("ALTER TABLE messages RENAME TO messages_good")
            conn.execute(
                "CREATE TABLE messages (id TEXT, chat_jid TEXT, timestamp TEXT)"
            )
            conn.execute(
                "INSERT INTO messages VALUES (?, ?, ?)",
                ("m1", TARGET, "2026-01-01 00:00:02"),
            )
        self.seed_state(pending_reasons=["message"], last_event_at=0.0)
        proc = self.run_worker()
        state = self.state_json()
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(state["worker_active"])
        self.assertIsNone(state["worker_pid"])
        self.assertIn("message", state["pending_reasons"])
        self.assertIn("OperationalError", state["last_error"])

    def test_retry_policy_backs_off_quota_and_pauses_bad_configuration(self):
        modulo = self.cargar_modulo(self.base / "agente-retry")
        self.assertEqual(modulo._retry_class("You've hit your usage limit"), "quota")
        self.assertEqual(
            [modulo._retry_delay("quota", n) for n in range(6)],
            [120.0, 240.0, 480.0, 960.0, 1800.0, 1800.0],
        )
        self.assertIsNone(modulo._retry_delay("config", 0))

    def test_the_rescue_does_not_duplicate_a_live_worker(self):
        self.seed_state(
            pending_reasons=["message"], last_event_at=0.0, worker_active=True, worker_pid=os.getpid()
        )
        watcher = subprocess.Popen([str(SCRIPT), "watch"], env=self.env)
        try:
            time.sleep(1.0)
        finally:
            watcher.terminate()
            watcher.wait(timeout=3)
        self.assertFalse(self.trace.exists(), "arranco un worker con otro vivo")
        self.assertEqual(self.state_json().get("retries", 0), 0)

    def copia_del_script(self):
        """Una copia para poder tocarla sin escribir en bin/evento-wsp.py."""
        copia = self.base / "evento.py"
        copia.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
        copia.chmod(0o755)
        return copia

    def runner_log(self):
        try:
            return (self.state / "runner.log").read_text(encoding="utf-8")
        except OSError:
            return ""

    def test_the_watcher_reloads_itself_when_the_script_changes(self):
        """El watcher no es hijo del bridge, asi que `wspbot` no lo reinicia. Sin esto un
        arreglo escrito en el script no llegaba a correr NUNCA: el 2026-08-25 el watcher
        de las 11:16 se quedo sin el rescate de cola y una vuelta estuvo parada 24 min."""
        copia = self.copia_del_script()
        watcher = subprocess.Popen([sys.executable, str(copia), "watch"], env=self.env)
        try:
            self.wait_for(lambda: "WATCH pid=" in self.runner_log())
            copia.write_text(
                copia.read_text(encoding="utf-8") + "\n# cambio en caliente\n", encoding="utf-8"
            )
            self.wait_for(lambda: "RECARGA el script cambio" in self.runner_log())
            self.wait_for(lambda: self.runner_log().count("WATCH pid=") == 2)
            arranques = [
                linea for linea in self.runner_log().splitlines() if "WATCH pid=" in linea
            ]
            # execv conserva el PID: es el mismo proceso con el codigo nuevo, y por eso
            # el watch.lock no se le escapa a nadie por el camino.
            self.assertEqual(arranques[0].split("pid=")[1], arranques[1].split("pid=")[1])
            time.sleep(0.3)
            self.assertIsNone(watcher.poll(), "el watcher murio al recargar")
        finally:
            watcher.terminate()
            watcher.wait(timeout=3)

    def test_a_broken_script_does_not_kill_the_watcher(self):
        """Un guardado a medias no puede llevarse por delante el unico proceso que avisa
        de que el bridge se cayo."""
        copia = self.copia_del_script()
        watcher = subprocess.Popen([sys.executable, str(copia), "watch"], env=self.env)
        try:
            self.wait_for(lambda: "WATCH pid=" in self.runner_log())
            copia.write_text("def roto(:\n", encoding="utf-8")
            self.wait_for(lambda: "RECARGA abortada" in self.runner_log())
            time.sleep(0.3)
            self.assertIsNone(watcher.poll(), "el watcher murio con el script roto")
            # Se queja una vez por guardado, no cada 5 segundos
            self.assertEqual(self.runner_log().count("RECARGA abortada"), 1)
        finally:
            watcher.terminate()
            watcher.wait(timeout=3)

    def cargar_modulo(self, agente_file):
        import importlib.util

        with unittest.mock.patch.dict(
            os.environ, {"WSP_AGENT_FILE": str(agente_file)}, clear=False
        ):
            os.environ.pop("WSP_AGENT", None)
            os.environ.pop("WSP_COMPACT_CADA_MIN", None)
            spec = importlib.util.spec_from_file_location("evento_wsp_bajo_prueba", SCRIPT)
            modulo = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(modulo)
            return modulo

    def test_the_engine_is_read_hot_and_not_cached_at_import(self):
        """El motor se leia UNA vez al importar. El watcher vive horas, asi que un
        `wspbot --claude` lo dejaba encolando `/compactar` con el motor de antes — y el
        worker lo corria como `claude -p "/compactar"`, que ahi no es ningun comando."""
        agente = self.base / "agente-caliente"
        agente.write_text("codex\n", encoding="utf-8")
        modulo = self.cargar_modulo(agente)
        self.assertEqual(modulo._agente(), "codex")
        self.assertEqual(modulo._compact_min(), 60.0)

        # Mismo proceso, sin reimportar: es justo lo que le pasa al watcher vivo
        agente.write_text("claude\n", encoding="utf-8")
        self.assertEqual(modulo._agente(), "claude")
        self.assertEqual(modulo._compact_min(), 0.0)

    def test_the_claude_code_session_of_the_launcher_does_not_leak_into_the_turn(self):
        """El watcher hereda el entorno de quien lo lanzo y despues vive horas. Si eso fue
        una sesion de Claude Code, su CLAUDE_CODE_SESSION_ID viajaba pegado hasta el
        `claude -p --resume` de la vuelta, que es OTRA sesion distinta."""
        env = self.env.copy()
        env["WSP_AGENT"] = "claude"
        env["CLAUDE_CODE_SESSION_ID"] = "de-otra-sesion"
        env["CLAUDECODE"] = "1"
        env["TEST_ENV_TRACE"] = str(self.base / "env.trace")
        self.fake_claude.write_text(
            self.fake_claude.read_text(encoding="utf-8")
            + "\nopen(os.environ['TEST_ENV_TRACE'], 'w', encoding='utf-8')"
            + ".write(repr(sorted(k for k in os.environ if k.startswith('CLAUDE'))))\n",
            encoding="utf-8",
        )
        self.insert("m1", "2026-01-01 00:00:02")
        self.seed_state(pending_reasons=["message"], last_event_at=0.0, worker_active=False)
        self.run_worker(env)
        heredadas = (self.base / "env.trace").read_text(encoding="utf-8")
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", heredadas)
        self.assertNotIn("CLAUDECODE", heredadas)

    def test_a_queued_compaction_is_dropped_when_the_engine_changed(self):
        """Un `compact` encolado por un watcher en codex no puede correrse despues como
        `claude -p "/compactar"`. Es una vuelta entera gastada en mandarle una barra."""
        env = self.env.copy()
        env["WSP_AGENT"] = "claude"
        env.pop("WSP_COMPACT_CADA_MIN", None)  # con claude el default ya es 0
        self.seed_state(pending_reasons=["compact"], last_event_at=0.0, worker_active=False)
        self.run_worker(env)
        self.assertEqual(self.claude_runs(), [], "corrio una compactacion con el motor claude")
        self.assertEqual(self.state_json()["compactions"], 0)
        self.assertEqual(self.state_json()["pending_reasons"], [])


if __name__ == "__main__":
    unittest.main()
