#!/usr/bin/env python3
"""Cola local por eventos para despertar el hilo de Codex de WSP Bot.

El bridge llama ``notify`` solo despues de persistir un mensaje entrante nuevo. Un
worker separado agrupa la rafaga y reanuda el hilo existente con ``codex exec
resume``. El estado y los locks viven fuera del proceso para sobrevivir reinicios.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator


BASE = Path(os.environ.get("WSP_BASE", "/home/kutex/WSP Bot"))
STATE_DIR = Path(os.environ.get("WSP_EVENT_STATE", BASE / "nucleo/eventos"))
DB = Path(os.environ.get("WSP_DB", BASE / "whatsapp-mcp/whatsapp-bridge/store/messages.db"))
CURSOR = Path(os.environ.get("WSP_CURSOR", BASE / "nucleo/cursor.txt"))
MUDO = Path(os.environ.get("WSP_MUDO", BASE / "nucleo/MUDO"))
TRASPASO = Path(os.environ.get("WSP_TRASPASO", BASE / "nucleo/.traspaso"))
JID = os.environ.get("WSP_JID", "000000000000000@lid")

AUTOMATION = Path(
    os.environ.get("WSP_CODEX_AUTOMATION", "/home/kutex/.codex/automations/wspbot/automation.toml")
)
AUTOMATIONS_DIR = Path(
    os.environ.get("WSP_CODEX_AUTOMATIONS_DIR", "/home/kutex/.codex/automations")
)
CODEX = os.environ.get("WSP_CODEX_BIN", "/usr/lib/chatgpt/resources/codex")
PROMPT = os.environ.get("WSP_CODEX_PROMPT", f"sigue al pie de la letra {BASE}/PROMPT.md")
PULSO = os.environ.get("WSP_PULSO", str(BASE / "bin/pulso.sh"))
REGISTRAR = os.environ.get("WSP_REGISTRAR", str(BASE / "bin/registrar.sh"))

DEBOUNCE = float(os.environ.get("WSP_EVENT_DEBOUNCE", "6"))
WATCH_INTERVAL = float(os.environ.get("WSP_EVENT_WATCH_INTERVAL", "5"))
SILENCE_MIN = int(os.environ.get("WSP_UMBRAL_SILENCIO", "30"))
PARENT_PID = int(os.environ.get("WSP_EVENT_PARENT_PID", "0") or 0)

STATE_FILE = STATE_DIR / "state.json"
STATE_LOCK = STATE_DIR / "state.lock"
WORKER_LOCK = STATE_DIR / "worker.lock"
WATCH_LOCK = STATE_DIR / "watch.lock"
RUN_LOG = STATE_DIR / "runner.log"


def _default_state() -> dict[str, Any]:
    return {
        "pending_reasons": [],
        "last_event_at": 0.0,
        "last_message_id": "",
        "worker_active": False,
        "worker_pid": None,
        "silence_woken_for": "",
        "mute_woken_for": "",
        "runs": 0,
        "quiet_mute_batches": 0,
        "suppressed_empty": 0,
        "last_error": "",
    }


def _ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)


@contextmanager
def _locked(path: Path, blocking: bool = True) -> Iterator[Any]:
    _ensure_state_dir()
    handle = path.open("a+")
    flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
    try:
        fcntl.flock(handle, flags)
    except BlockingIOError:
        handle.close()
        raise
    try:
        yield handle
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def _load_state() -> dict[str, Any]:
    state = _default_state()
    try:
        loaded = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            state.update(loaded)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return state


def _save_state(state: dict[str, Any]) -> None:
    _ensure_state_dir()
    tmp = STATE_FILE.with_name(f".{STATE_FILE.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE_FILE)


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _append_log(message: str) -> None:
    _ensure_state_dir()
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with RUN_LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"[{stamp}] {message}\n")


def _spawn_worker() -> None:
    env = os.environ.copy()
    cmd = [sys.executable, str(Path(__file__).resolve()), "worker"]
    if os.environ.get("WSP_EVENT_FOREGROUND") == "1":
        raise RuntimeError("foreground")
    _ensure_state_dir()
    log = RUN_LOG.open("a", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=BASE,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        log.close()
    with _locked(STATE_LOCK):
        state = _load_state()
        if state.get("worker_active") and not state.get("worker_pid"):
            state["worker_pid"] = proc.pid
            _save_state(state)


def queue(reason: str, message_id: str = "") -> bool:
    """Deja una sola vuelta pendiente y arranca el worker si hace falta."""
    start = False
    with _locked(STATE_LOCK):
        state = _load_state()
        # ``None`` means the notifier already reserved the worker and is between
        # Popen() and publishing its pid. Treating that tiny window as a dead process
        # would let two simultaneous messages spawn two workers.
        if (
            state.get("worker_active")
            and state.get("worker_pid") is not None
            and not _pid_alive(state.get("worker_pid"))
        ):
            state["worker_active"] = False
            state["worker_pid"] = None
        reasons = set(state.get("pending_reasons") or [])
        reasons.add(reason)
        state["pending_reasons"] = sorted(reasons)
        if reason == "message":
            state["last_event_at"] = time.time()
            state["last_message_id"] = message_id
        if not state.get("worker_active"):
            state["worker_active"] = True
            state["worker_pid"] = None
            start = True
        _save_state(state)

    if start:
        try:
            _spawn_worker()
        except RuntimeError as exc:
            if str(exc) == "foreground":
                worker()
            else:
                raise
        except Exception as exc:
            with _locked(STATE_LOCK):
                state = _load_state()
                state["worker_active"] = False
                state["worker_pid"] = None
                state["last_error"] = f"no se pudo arrancar worker: {exc}"
                _save_state(state)
            _append_log(f"ERROR no se pudo arrancar worker: {exc}")
            return False
    return True


def _read_cursor(name: str, fallback: str = "") -> str:
    try:
        for line in CURSOR.read_text(encoding="utf-8").splitlines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1]
    except OSError:
        pass
    return fallback


def _last_seen() -> str:
    ginger = _read_cursor("LAST_GINGER_TS", "1970-01-01 00:00:00")
    return _read_cursor("LAST_VISTO_TS", ginger) or ginger


def _db_value(sql: str, params: tuple[Any, ...] = ()) -> Any:
    with sqlite3.connect(DB, timeout=3) as conn:
        row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


def _new_incoming_count() -> int:
    if not DB.exists():
        return 0
    return int(
        _db_value(
            """
            SELECT count(*) FROM messages
            WHERE chat_jid = ? AND is_from_me = 0
              AND substr(timestamp, 1, 19) > ?
            """,
            (JID, _last_seen()),
        )
        or 0
    )


def _new_message_count() -> int:
    if not DB.exists():
        return 0
    return int(
        _db_value(
            """
            SELECT count(*) FROM messages
            WHERE chat_jid = ? AND substr(timestamp, 1, 19) > ?
            """,
            (JID, _last_seen()),
        )
        or 0
    )


def _new_exact_on() -> bool:
    if not DB.exists():
        return False
    return bool(
        _db_value(
            """
            SELECT 1 FROM messages
            WHERE chat_jid = ? AND substr(timestamp, 1, 19) > ?
              AND trim(coalesce(content, '')) = '/on'
            LIMIT 1
            """,
            (JID, _last_seen()),
        )
    )


def _latest_chat_timestamp() -> str:
    if not DB.exists():
        return ""
    return str(
        _db_value(
            "SELECT max(substr(timestamp, 1, 19)) FROM messages WHERE chat_jid = ?",
            (JID,),
        )
        or ""
    )


def _timestamp_epoch(value: str) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return 0.0


def _mute_deadline() -> tuple[str, float]:
    try:
        for line in MUDO.read_text(encoding="utf-8").splitlines():
            if line.startswith("HASTA:"):
                raw = line.split(":", 1)[1].strip()
                return raw, _timestamp_epoch(raw)
    except OSError:
        pass
    return "", 0.0


def _read_automation(path: Path) -> dict[str, Any]:
    try:
        import tomllib

        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ImportError):
        return {}
    return data if isinstance(data, dict) else {}


def _automation_config() -> tuple[str, str]:
    thread = os.environ.get("WSP_CODEX_THREAD_ID", "")
    status = ""
    if AUTOMATION.exists():
        data = _read_automation(AUTOMATION)
        thread = thread or str(data.get("target_thread_id", ""))
        status = str(data.get("status", ""))
    return thread, status


def _active_automations_on_thread(thread: str) -> list[str]:
    """Cualquier automation ACTIVE sobre el mismo hilo corre fuera de este lock.

    El scheduler de la app no respeta ``worker.lock``: si dispara ``codex exec`` sobre el
    mismo ``target_thread_id`` mientras el worker trabaja, la vuelta del bot falla. Mirar
    solo ``wspbot`` dejaba pasar a ``wspbot-2`` y a cualquier otra creada despues.
    """
    if not thread or not AUTOMATIONS_DIR.is_dir():
        return []
    conflicts = []
    for path in sorted(AUTOMATIONS_DIR.glob("*/automation.toml")):
        data = _read_automation(path)
        if str(data.get("target_thread_id", "")) != thread:
            continue
        if str(data.get("status", "")).upper() != "ACTIVE":
            continue
        conflicts.append(str(data.get("id") or path.parent.name))
    return conflicts


def _run_agent() -> tuple[bool, str]:
    thread, automation_status = _automation_config()
    if not thread:
        return False, "falta target_thread_id o WSP_CODEX_THREAD_ID"
    if os.environ.get("WSP_ALLOW_ACTIVE_AUTOMATION") != "1":
        conflicts = _active_automations_on_thread(thread)
        if automation_status.upper() == "ACTIVE" and not conflicts:
            conflicts = ["wspbot"]
        if conflicts:
            return False, (
                "hay automations ACTIVE sobre el mismo hilo: "
                + ", ".join(conflicts)
                + "; pausalas para evitar ejecuciones paralelas"
            )
    if not Path(CODEX).is_file():
        return False, f"no existe Codex CLI: {CODEX}"

    cmd = [CODEX, "exec", "resume", "--all", thread, PROMPT]
    _append_log(f"RUN thread={thread}")
    with RUN_LOG.open("a", encoding="utf-8") as log:
        result = subprocess.run(
            cmd,
            cwd=BASE,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
    if result.returncode != 0:
        return False, f"codex exec resume termino con {result.returncode}"
    return True, ""


def _quiet_mute_batch(count: int) -> tuple[bool, str]:
    """Replica la vuelta MUDO de PROMPT sin arrancar el modelo."""
    try:
        pulso = subprocess.run(
            [PULSO], cwd=BASE, text=True, capture_output=True, check=False
        )
    except OSError as exc:
        return False, f"no se pudo correr pulso en MUDO: {exc}"
    if pulso.returncode != 0:
        return False, f"pulso en MUDO termino con {pulso.returncode}: {pulso.stderr.strip()}"
    if "MUDO: acaba de expirar" in pulso.stdout or "MUDO: no" in pulso.stdout:
        return _run_agent()
    if "ESTADO: BRIDGE_CAIDO" in pulso.stdout:
        return False, "bridge caido durante la vuelta MUDO"

    cmd = [REGISTRAR, "--ts", "visto", "--log", f"MUDO — {count} mensajes nuevos"]
    reg = subprocess.run(cmd, cwd=BASE, text=True, capture_output=True, check=False)
    if reg.returncode != 0:
        return False, f"registrar MUDO termino con {reg.returncode}: {reg.stderr.strip()}"
    _append_log(f"MUDO local count={count} sin modelo")
    return True, ""


def _claim_after_debounce() -> tuple[list[str], float]:
    while True:
        with _locked(STATE_LOCK):
            state = _load_state()
            reasons = list(state.get("pending_reasons") or [])
            last_event = float(state.get("last_event_at") or 0.0)
        if not reasons:
            return [], 0.0
        remaining = DEBOUNCE - (time.time() - last_event) if "message" in reasons else 0.0
        if remaining > 0:
            time.sleep(min(remaining, 1.0))
            continue
        with _locked(STATE_LOCK):
            state = _load_state()
            current_reasons = list(state.get("pending_reasons") or [])
            current_event = float(state.get("last_event_at") or 0.0)
            if "message" in current_reasons and time.time() - current_event < DEBOUNCE:
                continue
            state["pending_reasons"] = []
            _save_state(state)
            return current_reasons, current_event


def _silence_due() -> bool:
    latest = _latest_chat_timestamp()
    epoch = _timestamp_epoch(latest)
    return bool(epoch and time.time() - epoch >= SILENCE_MIN * 60)


def _finish_worker(error: str = "") -> None:
    with _locked(STATE_LOCK):
        state = _load_state()
        state["worker_active"] = False
        state["worker_pid"] = None
        state["last_error"] = error
        _save_state(state)


def worker() -> int:
    _ensure_state_dir()
    with _locked(WORKER_LOCK):
        with _locked(STATE_LOCK):
            state = _load_state()
            state["worker_active"] = True
            state["worker_pid"] = os.getpid()
            _save_state(state)

        while True:
            reasons, _ = _claim_after_debounce()
            if not reasons:
                _finish_worker()
                return 0

            incoming = _new_incoming_count()
            event_actionable = "message" in reasons and incoming > 0
            timed_actionable = "mute_expired" in reasons and MUDO.exists()
            silence_actionable = "silence" in reasons and not MUDO.exists() and _silence_due()
            if not (event_actionable or timed_actionable or silence_actionable):
                with _locked(STATE_LOCK):
                    state = _load_state()
                    state["suppressed_empty"] = int(state.get("suppressed_empty") or 0) + 1
                    _save_state(state)
                _append_log(f"SKIP vacio reasons={','.join(reasons)}")
                continue

            if MUDO.exists() and event_actionable and not _new_exact_on():
                _, deadline = _mute_deadline()
                if not deadline or deadline > time.time():
                    ok, error = _quiet_mute_batch(_new_message_count())
                    if ok:
                        with _locked(STATE_LOCK):
                            state = _load_state()
                            state["quiet_mute_batches"] = int(state.get("quiet_mute_batches") or 0) + 1
                            state["last_error"] = ""
                            _save_state(state)
                        continue
                    with _locked(STATE_LOCK):
                        state = _load_state()
                        state["pending_reasons"] = sorted(set(state.get("pending_reasons") or []) | set(reasons))
                        _save_state(state)
                    _finish_worker(error)
                    _append_log(f"ERROR {error}")
                    return 1

            ok, error = _run_agent()
            if not ok:
                with _locked(STATE_LOCK):
                    state = _load_state()
                    state["pending_reasons"] = sorted(set(state.get("pending_reasons") or []) | set(reasons))
                    _save_state(state)
                _finish_worker(error)
                _append_log(f"ERROR {error}")
                return 1
            with _locked(STATE_LOCK):
                state = _load_state()
                state["runs"] = int(state.get("runs") or 0) + 1
                state["last_error"] = ""
                _save_state(state)


def _mark_and_queue(reason: str, marker: str, value: str) -> None:
    should_queue = False
    with _locked(STATE_LOCK):
        state = _load_state()
        if state.get(marker) != value:
            state[marker] = value
            _save_state(state)
            should_queue = True
    if should_queue:
        queue(reason)


def _parent_alive() -> bool:
    return PARENT_PID <= 0 or _pid_alive(PARENT_PID)


def watch() -> int:
    _ensure_state_dir()
    try:
        lock = _locked(WATCH_LOCK, blocking=False)
        lock.__enter__()
    except BlockingIOError:
        return 0
    try:
        _append_log(f"WATCH pid={os.getpid()} parent={PARENT_PID or '-'}")
        while _parent_alive():
            if MUDO.exists():
                raw, deadline = _mute_deadline()
                if raw and deadline and deadline <= time.time():
                    _mark_and_queue("mute_expired", "mute_woken_for", raw)
            else:
                latest = _latest_chat_timestamp()
                if latest and _silence_due():
                    _mark_and_queue("silence", "silence_woken_for", latest)
            time.sleep(max(WATCH_INTERVAL, 0.1))
        return 0
    finally:
        lock.__exit__(None, None, None)


def notify(args: argparse.Namespace) -> int:
    if args.chat_jid != JID or args.from_me:
        return 0
    return 0 if queue("message", args.message_id) else 1


def doctor() -> int:
    thread, status = _automation_config()
    problems = []
    if not DB.is_file():
        problems.append(f"falta DB: {DB}")
    if not Path(CODEX).is_file():
        problems.append(f"falta Codex: {CODEX}")
    if not thread:
        problems.append("falta target_thread_id")
    conflicts = _active_automations_on_thread(thread)
    if status.upper() == "ACTIVE" and "wspbot" not in conflicts:
        conflicts.append("wspbot")
    if conflicts:
        problems.append(
            "automations ACTIVE sobre el mismo hilo: "
            + ", ".join(conflicts)
            + "; pausalas antes de activar eventos"
        )
    print(f"JID={JID}")
    print(f"THREAD={thread or '-'}")
    print(f"AUTOMATION={status or '-'}")
    print(f"ACTIVE_EN_HILO={', '.join(conflicts) if conflicts else '-'}")
    print(f"DEBOUNCE={DEBOUNCE:g}s")
    print(f"SILENCE={SILENCE_MIN}m")
    if problems:
        for problem in problems:
            print(f"ERROR: {problem}")
        return 1
    print("OK")
    return 0


def status() -> int:
    with _locked(STATE_LOCK):
        print(json.dumps(_load_state(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    notify_parser = sub.add_parser("notify", help="encolar un mensaje entrante nuevo")
    notify_parser.add_argument("--chat-jid", required=True)
    notify_parser.add_argument("--message-id", default="")
    notify_parser.add_argument("--from-me", action="store_true")
    sub.add_parser("worker", help="procesar la cola")
    sub.add_parser("watch", help="vigilar vencimientos de mudo y silencio")
    sub.add_parser("doctor", help="validar configuracion sin despertar el modelo")
    sub.add_parser("status", help="mostrar estado de la cola")
    args = parser.parse_args()
    if args.command == "notify":
        return notify(args)
    if args.command == "worker":
        return worker()
    if args.command == "watch":
        return watch()
    if args.command == "doctor":
        return doctor()
    return status()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda _signum, _frame: sys.exit(0))
    raise SystemExit(main())
