import os
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "bin/wspbot"


class WspbotCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.bridge_dir = self.base / "bridge"
        self.bridge_dir.mkdir()
        self.fake_bin = self.base / "bin"
        self.fake_bin.mkdir()
        self.trace = self.base / "evento.trace"

        self.evento = self.base / "evento.py"
        self.evento.write_text(
            """#!/usr/bin/env python3
import os, sys
open(os.environ['TEST_EVENTO_TRACE'], 'a', encoding='utf-8').write(' '.join(sys.argv[1:]) + '\\n')
if sys.argv[1] == 'doctor':
    print('AGENT=codex')
    print('OK')
elif sys.argv[1] == 'bridge-starting':
    print('BRIDGE=STARTING')
elif sys.argv[1] == 'status':
    print('{}')
""",
            encoding="utf-8",
        )
        self.evento.chmod(0o755)

        bridge = self.bridge_dir / "whatsapp-client"
        bridge.write_text(
            """#!/usr/bin/env bash
echo BRIDGE_EXECUTED
echo "EVENT_TRIGGER=[$WSP_EVENT_TRIGGER]"
echo "AGENT_ENV=[${WSP_AGENT:-}]"
""",
            encoding="utf-8",
        )
        bridge.chmod(0o755)

        pgrep = self.fake_bin / "pgrep"
        pgrep.write_text(
            """#!/usr/bin/env bash
case "$*" in
  *whatsapp-client*) [ -n "${TEST_BRIDGE_PID:-}" ] && echo "$TEST_BRIDGE_PID" && exit 0 ;;
  *" worker"*) [ -n "${TEST_WORKER_PID:-}" ] && echo "$TEST_WORKER_PID" && exit 0 ;;
  *" watch"*) [ -n "${TEST_WATCHER_PID:-}" ] && echo "$TEST_WATCHER_PID" && exit 0 ;;
esac
exit 1
""",
            encoding="utf-8",
        )
        pgrep.chmod(0o755)

        curl = self.fake_bin / "curl"
        curl.write_text(
            """#!/usr/bin/env bash
case "${TEST_HEALTH_MODE:-down}" in
  connected) printf '{"connected":true,"logged_in":true}\\n200' ;;
  disconnected) printf '{"connected":false,"logged_in":true}\\n200' ;;
  logged-out) printf '{"connected":false,"logged_in":false}\\n200' ;;
  legacy) printf '404 page not found\\n404' ;;
  *) printf '\\n000' ;;
esac
""",
            encoding="utf-8",
        )
        curl.chmod(0o755)

        self.env = os.environ.copy()
        self.env.update(
            {
                "PATH": str(self.fake_bin) + os.pathsep + self.env["PATH"],
                "WSP_BASE": str(self.base),
                "WSP_BRIDGE_DIR": str(self.bridge_dir),
                "WSP_EVENTO": str(self.evento),
                "WSP_AGENT_FILE": str(self.base / "agente"),
                "TEST_EVENTO_TRACE": str(self.trace),
            }
        )

    def tearDown(self):
        self.tmp.cleanup()

    def run_wspbot(self, *args, **extra_env):
        env = self.env.copy()
        env.update(extra_env)
        return subprocess.run(
            [str(SCRIPT), *args], env=env, text=True, capture_output=True
        )

    def test_plain_start_executes_foreground_bridge_without_ai(self):
        proc = self.run_wspbot()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("arrancando sin IA", proc.stdout)
        self.assertIn("deja esta terminal abierta", proc.stdout)
        self.assertIn("BRIDGE_EXECUTED", proc.stdout)
        self.assertIn("EVENT_TRIGGER=[]", proc.stdout)
        self.assertFalse(self.trace.exists())

    def test_ai_modes_persist_engine_and_start_coordinator(self):
        for motor in ("codex", "claude"):
            with self.subTest(motor=motor):
                self.trace.unlink(missing_ok=True)
                proc = self.run_wspbot(f"--{motor}")

                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual((self.base / "agente").read_text(), motor + "\n")
                self.assertIn(f"AGENT_ENV=[{motor}]", proc.stdout)
                self.assertIn(f"EVENT_TRIGGER=[{self.evento}]", proc.stdout)
                self.assertEqual(
                    self.trace.read_text().splitlines(), ["doctor", "bridge-starting"]
                )

    def test_conflicts_do_not_change_saved_engine(self):
        agent = self.base / "agente"
        conflicts = (
            ({"TEST_BRIDGE_PID": "4101"}, "bridge"),
            ({"TEST_WORKER_PID": "4102"}, "vuelta de IA"),
            ({"TEST_WATCHER_PID": "4103"}, "watcher de IA"),
        )
        for env, expected in conflicts:
            with self.subTest(expected=expected):
                agent.write_text("codex\n", encoding="utf-8")
                proc = self.run_wspbot("--claude", **env)

                self.assertEqual(proc.returncode, 1)
                self.assertIn(expected, proc.stderr)
                self.assertEqual(agent.read_text(), "codex\n")

    def test_stop_ai_is_idempotent_and_refuses_active_session(self):
        proc = self.run_wspbot("--stop-ia")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ya estaba apagada", proc.stdout)

        for env, expected in (
            ({"TEST_BRIDGE_PID": "4201"}, "bridge"),
            ({"TEST_WORKER_PID": "4202"}, "vuelta de IA"),
        ):
            with self.subTest(expected=expected):
                proc = self.run_wspbot("--stop-ia", **env)
                self.assertEqual(proc.returncode, 1)
                self.assertIn(expected, proc.stderr)

    def test_stop_ai_kills_idle_watcher(self):
        watcher = subprocess.Popen(["sleep", "30"])
        try:
            proc = self.run_wspbot("--stop-ia", TEST_WATCHER_PID=str(watcher.pid))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("IA apagada", proc.stdout)
            watcher.wait(timeout=2)
            self.assertEqual(watcher.returncode, -signal.SIGTERM)
        finally:
            if watcher.poll() is None:
                watcher.terminate()
                watcher.wait(timeout=2)

        failed = self.run_wspbot("--stop-ia", TEST_WATCHER_PID="99999999")
        self.assertEqual(failed.returncode, 1)
        self.assertIn("no pude apagar", failed.stderr)

    def test_status_distinguishes_bridge_health_states(self):
        cases = (
            ({}, "apagado"),
            ({"TEST_BRIDGE_PID": "4321"}, "arrancando"),
            ({"TEST_BRIDGE_PID": "4321", "TEST_HEALTH_MODE": "connected"}, "conectado"),
            (
                {"TEST_BRIDGE_PID": "4321", "TEST_HEALTH_MODE": "disconnected"},
                "desconectado de WhatsApp",
            ),
            (
                {"TEST_BRIDGE_PID": "4321", "TEST_HEALTH_MODE": "logged-out"},
                "sesion cerrada",
            ),
        )
        for env, expected in cases:
            with self.subTest(expected=expected):
                proc = self.run_wspbot("--status", **env)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn(expected, proc.stdout + proc.stderr)

        off = self.run_wspbot("--status")
        self.assertIn("ia:      apagada", off.stdout)

        on = self.run_wspbot("--status", TEST_WATCHER_PID="4301")
        self.assertIn("ia:      activa", on.stdout)


if __name__ == "__main__":
    unittest.main()
