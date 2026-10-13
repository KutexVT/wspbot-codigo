#!/usr/bin/env python3
"""Cola local por eventos para despertar el hilo de Codex de WSP Bot.

El bridge llama ``notify`` solo despues de persistir un mensaje nuevo del chat. Un
worker separado agrupa la rafaga y reanuda el hilo existente con ``codex exec
resume``. El estado y los locks viven fuera del proceso para sobrevivir reinicios.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator


BASE = Path(os.environ.get("WSP_BASE", "/home/kutex/WSP Bot"))
STATE_DIR = Path(os.environ.get("WSP_EVENT_STATE", BASE / "nucleo/eventos"))
DB = Path(os.environ.get("WSP_DB", BASE / "whatsapp-mcp/whatsapp-bridge/store/messages.db"))
CURSOR = Path(os.environ.get("WSP_CURSOR", BASE / "nucleo/cursor.txt"))
MUDO = Path(os.environ.get("WSP_MUDO", BASE / "nucleo/MUDO"))
JID = os.environ.get("WSP_JID", "000000000000000@lid")
DIRECT_WINDOW = 120.0

AUTOMATION = Path(
    os.environ.get("WSP_CODEX_AUTOMATION", "/home/kutex/.codex/automations/wspbot/automation.toml")
)
AUTOMATIONS_DIR = Path(
    os.environ.get("WSP_CODEX_AUTOMATIONS_DIR", "/home/kutex/.codex/automations")
)
CODEX_LOCKS = Path(
    os.environ.get("WSP_CODEX_LOCKS", "/home/kutex/.codex/thread-writer-locks")
)
CODEX = os.environ.get("WSP_CODEX_BIN", "/usr/lib/chatgpt/resources/codex")
CODEX_MODEL = "gpt-5.6-sol"
CODEX_EFFORT = "medium"

# Motor del agente: "codex" (el hilo de la app de ChatGPT) o "claude" (Claude Code).
# Son sesiones distintas y el contexto NO se migra de uno al otro.
AGENT_FILE = Path(os.environ.get("WSP_AGENT_FILE", STATE_DIR / "agente"))


def _leer_agente() -> str:
    """El motor sale del entorno, del disco o del default, en ese orden.

    Guardarlo en disco es lo que hace que `doctor` diga la verdad desde CUALQUIER
    terminal. Leyendolo solo del entorno, una shell sin el export contestaba "codex"
    mientras el bridge corria con Claude, y no habia forma de notarlo.

    El entorno gana igual, que es la valvula de escape para probar sin tocar la eleccion
    guardada.
    """
    desde_env = os.environ.get("WSP_AGENT", "").strip().lower()
    if desde_env:
        return desde_env
    try:
        guardado = AGENT_FILE.read_text(encoding="utf-8").strip().lower()
    except OSError:
        guardado = ""
    # Un archivo con basura no puede dejar el bot sin arrancar: se cae al default y
    # doctor ya se queja aparte si el motor no existe.
    return guardado if guardado in ("codex", "claude") else "codex"


def _agente() -> str:
    """El motor SIEMPRE en caliente, nunca cacheado en una constante de import.

    El watcher sobrevive al bridge a proposito, asi que un `wspbot --claude` no lo
    reinicia: con el motor congelado al arrancar seguia encolando `compact` cada hora
    aunque el motor ya fuera claude, y el worker lo corria como `claude -p "/compactar"`,
    que ahi no es ningun comando.
    """
    return _leer_agente()


CLAUDE = os.environ.get("WSP_CLAUDE_BIN", str(Path.home() / ".local/bin/claude"))
CLAUDE_MODEL = os.environ.get("WSP_CLAUDE_MODEL", "")
# Sin esto la vuelta se cuelga esperando una confirmacion que nadie va a dar: no hay
# terminal al otro lado.
CLAUDE_FLAGS = os.environ.get("WSP_CLAUDE_FLAGS", "--dangerously-skip-permissions").split()
PROMPT = os.environ.get("WSP_CODEX_PROMPT", f"sigue al pie de la letra {BASE}/PROMPT.md")
PULSO = os.environ.get("WSP_PULSO", str(BASE / "bin/pulso.sh"))
REGISTRAR = os.environ.get("WSP_REGISTRAR", str(BASE / "bin/registrar.sh"))

DEBOUNCE = float(os.environ.get("WSP_EVENT_DEBOUNCE", "6"))
WATCH_INTERVAL = float(os.environ.get("WSP_EVENT_WATCH_INTERVAL", "5"))
SILENCE_MIN = int(os.environ.get("WSP_UMBRAL_SILENCIO", "30"))
PARENT_PID = int(os.environ.get("WSP_EVENT_PARENT_PID", "0") or 0)

# Salud del bridge. Sin heartbeat nadie mas la vigila: pulso.sh solo corre cuando ya hay
# una vuelta, y con el bridge caido no llegan eventos que la disparen.
SALUD_URL = os.environ.get("WSP_SALUD_URL", "http://127.0.0.1:8080/api/health")
HEALTH_INTERVAL = float(os.environ.get("WSP_EVENT_HEALTH_INTERVAL", "60"))
AVISO_CADA_MIN = float(os.environ.get("WSP_AVISO_CADA_MIN", "15"))
# Gracia antes del primer aviso. El bridge arranca el watcher ANTES de levantar el HTTP,
# asi que la primera consulta siempre falla: sin esta espera cada `wspbot` disparaba una
# notificacion critica de mentira.
GRACIA_CAIDA = float(os.environ.get("WSP_EVENT_GRACIA", "45"))
# Cada cuanto se reintenta una vuelta que quedo colgada por un fallo transitorio.
REINTENTO = float(os.environ.get("WSP_EVENT_REINTENTO", "120"))
MARCA_AVISO = Path(os.environ.get("WSP_MARCA_AVISO", BASE / "nucleo/.aviso_bridge"))
NOTIFY_SEND = os.environ.get("WSP_NOTIFY_SEND", "notify-send")

# Compactacion del hilo. La hacia la automation `wspbot-2` cada hora, fuera de este lock
# y sobre el mismo target_thread_id: por eso podia chocar con una vuelta en curso.
# Codex y Claude gestionan su ventana solos, asi que la compactacion manual sobra y va
# desactivada salvo que se pida a mano.
def _compacta_a_mano() -> bool:
    """Si el motor de AHORA necesita que le manden `/compactar`.

    Va aparte del intervalo a proposito: `WSP_COMPACT_CADA_MIN=0` es "no la programes"
    y esto es "este motor no la entiende". Un `compact` que dejo encolado un watcher en
    codex no puede acabar corriendose como `claude -p "/compactar"`, que ahi no es
    ningun comando — seria una vuelta entera gastada en mandarle una barra al modelo.
    """
    return _agente() != "claude"


def _compact_min() -> float:
    """Minutos entre compactaciones manuales; cero delega el contexto al agente."""
    crudo = os.environ.get("WSP_COMPACT_CADA_MIN", "").strip()
    if crudo:
        try:
            return float(crudo)
        except ValueError:
            pass
    # Codex y Claude ya gestionan su contexto. La compactacion manual queda solo como
    # escape explicito para no gastar una vuelta cada hora cuando el chat esta quieto.
    return 0.0


COMPACT_PROMPT = os.environ.get("WSP_COMPACT_PROMPT", "/compactar")

STATE_FILE = STATE_DIR / "state.json"
# El uuid de la sesion de Claude Code. Se crea la primera vez y se reanuda siempre.
# Borrarlo es empezar de cero: se pierde todo el contexto acumulado del bot.
CLAUDE_SESSION = Path(os.environ.get("WSP_CLAUDE_SESSION", STATE_DIR / "claude-session"))
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
        "last_run_at": 0.0,
        "compactions": 0,
        "last_compact_at": 0.0,
        "bridge_down_since": 0.0,
        "last_retry_at": 0.0,
        "retries": 0,
        "quiet_mute_batches": 0,
        "quiet_mikel_batches": 0,
        "direct_until": 0.0,
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


def _entorno_limpio() -> dict[str, str]:
    """El entorno sin las marcas de la sesion que arranco esto.

    El watcher hereda el entorno de quien lo lanzo y despues vive horas. Si eso fue una
    sesion de Claude Code, su CLAUDE_CODE_SESSION_ID viaja pegado hasta el
    `claude -p --resume` de la vuelta, que es OTRA sesion distinta.
    """
    env = os.environ.copy()
    for clave in list(env):
        if clave.startswith("CLAUDE_CODE_") or clave in (
            "CLAUDECODE",
            "CLAUDE_PID",
            "CLAUDE_EFFORT",
            "AI_AGENT",
        ):
            env.pop(clave, None)
    return env


def _mtime_script() -> float:
    try:
        return Path(__file__).resolve().stat().st_mtime
    except OSError:
        return 0.0


def _recargar_si_cambio(referencia: float) -> float:
    """Se reemplaza a si mismo cuando el script cambia en disco.

    El watcher NO es hijo del bridge, asi que `wspbot` no lo reinicia y un arreglo escrito
    aqui no llegaba a correr nunca: el proceso vivo seguia con el codigo de cuando
    arranco. Paso de verdad el 2026-08-25 — el rescate de cola estaba escrito y el watcher
    de las 11:16 no lo tenia, con una vuelta parada 24 minutos.

    Se compila antes de saltar: un guardado a medias mataria al watcher, y con el se va el
    unico aviso de que el bridge se cayo. Si no compila se devuelve el mtime igual, para
    quejarse una vez y no cada 5 segundos.
    """
    actual = _mtime_script()
    if not actual or actual == referencia:
        return referencia
    ruta = str(Path(__file__).resolve())
    try:
        # compile() a secas y no py_compile: solo hace falta validar la sintaxis, y
        # py_compile ademas quiere ESCRIBIR el .pyc, que es un fallo distinto.
        compile(Path(ruta).read_text(encoding="utf-8"), ruta, "exec")
    except (SyntaxError, ValueError, OSError) as exc:
        _append_log(f"RECARGA abortada, el script no compila: {str(exc).strip()[:200]}")
        return actual
    _append_log("RECARGA el script cambio en disco, arrancando de nuevo")
    try:
        os.execv(sys.executable, [sys.executable, ruta, "watch"])
    except OSError as exc:
        _append_log(f"RECARGA fallo el execv: {exc}")
    return actual


def _spawn_worker() -> None:
    env = _entorno_limpio()
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


def queue(reason: str, message_id: str = "", direct: bool = False) -> bool:
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
            if direct:
                state["direct_until"] = time.time() + DIRECT_WINDOW
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


def _new_message_count() -> int:
    if not DB.exists():
        return 0
    return int(
        _db_value(
            """
            SELECT count(*) FROM messages
            WHERE chat_jid = ? AND coalesce(sender, '') <> '__tulpa__'
              AND substr(timestamp, 1, 19) > ?
            """,
            (JID, _last_seen()),
        )
        or 0
    )


def _new_messages() -> list[tuple[str, bool, str]]:
    if not DB.exists():
        return []
    with sqlite3.connect(DB, timeout=3) as conn:
        return [
            (str(row[0] or ""), bool(row[1]), str(row[2] or ""))
            for row in conn.execute(
                """
                SELECT content, is_from_me, media_type FROM messages
                WHERE chat_jid = ? AND coalesce(sender, '') <> '__tulpa__'
                  AND substr(timestamp, 1, 19) > ?
                ORDER BY timestamp, id
                """,
                (JID, _last_seen()),
            )
        ]


def _direct_text(content: str) -> bool:
    text = content.strip().casefold()
    return bool(
        re.match(r"^/(?:on|off|sticker|sys)(?:\s|$)", text)
        or re.search(r"(?<!\w)tulpa(?!\w)", text)
    )


def _message_batch_needs_agent() -> bool:
    """Contacto siempre despierta; lo de Mikel solo si va dirigido o es ambiguo."""
    rows = _new_messages()
    if not rows or any(not from_me for _, from_me, _ in rows):
        return bool(rows)

    now = time.time()
    with _locked(STATE_LOCK):
        state = _load_state()
        direct = now < float(state.get("direct_until") or 0.0)
        direct = direct or any(_direct_text(content) or media for content, _, media in rows)
        if direct:
            state["direct_until"] = now + DIRECT_WINDOW
            _save_state(state)
    return direct


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


def _bridge_health() -> tuple[bool, str]:
    """Misma lectura que ``salud_bridge`` de pulso.sh, con los mismos casos.

    Se pregunta al endpoint y no a la base: la base no distingue "no ha escrito nadie" de
    "no me estan llegando los mensajes". Un 404 es el binario viejo sin ``/api/health``,
    que esta vivo igual.
    """
    try:
        with urllib.request.urlopen(SALUD_URL, timeout=2) as resp:
            body = resp.read().decode("utf-8", "replace")
        if '"logged_in":false' in body:
            return False, "la sesion esta cerrada: hay que reescanear el QR (reiniciar no sirve)"
        if '"connected":false' in body:
            return False, "el bridge corre pero esta desconectado de WhatsApp"
        return True, ""
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return True, ""
        return False, f"el bridge contesta HTTP {exc.code}"
    except (urllib.error.URLError, OSError):
        return False, "el puerto 8080 no responde: el bridge no esta corriendo"


def _notify_bridge_down(reason: str) -> None:
    """Repite el aviso de escritorio de pulso.sh, que sin heartbeat ya no sale solo.

    Comparte ``nucleo/.aviso_bridge`` con pulso.sh a proposito: los dos respetan el mismo
    espaciado y pulso.sh borra la marca en cuanto el bridge vuelve.
    """
    try:
        age_min = (time.time() - MARCA_AVISO.stat().st_mtime) / 60
        if age_min < AVISO_CADA_MIN:
            return
    except OSError:
        pass
    try:
        subprocess.run(
            [
                NOTIFY_SEND,
                "-u",
                "critical",
                "WSP Bot — bridge caido",
                f"{reason}\nLevantalo con:  wspbot",
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass
    try:
        MARCA_AVISO.parent.mkdir(parents=True, exist_ok=True)
        MARCA_AVISO.touch()
    except OSError:
        pass
    _append_log(f"BRIDGE CAIDO {reason}")


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


def _claude_command(prompt: str) -> tuple[list[str] | None, str, str]:
    """Arma el `claude -p` equivalente y devuelve (cmd, id_de_sesion, error).

    La primera vuelta fija el uuid con --session-id y las siguientes lo reanudan con
    --resume. El uuid vive en un archivo y no en el estado, para que borrarlo sea un gesto
    explicito: es la unica forma de empezar de cero, y con ella se pierde el contexto.
    """
    if not Path(CLAUDE).is_file():
        return None, "", f"no existe Claude Code: {CLAUDE}"
    try:
        session = CLAUDE_SESSION.read_text(encoding="utf-8").strip()
    except OSError:
        session = ""
    if session:
        selector = ["--resume", session]
    else:
        session = str(uuid.uuid4())
        selector = ["--session-id", session]
        try:
            _ensure_state_dir()
            CLAUDE_SESSION.write_text(session + "\n", encoding="utf-8")
        except OSError as exc:
            return None, "", f"no se pudo guardar la sesion de Claude: {exc}"
    cmd = [CLAUDE, "-p", *CLAUDE_FLAGS]
    if CLAUDE_MODEL:
        cmd += ["--model", CLAUDE_MODEL]
    return cmd + selector + [prompt], session, ""


def _thread_writer_pid(thread: str) -> int:
    """PID que tiene tomado el writer del hilo de Codex, o 0 si esta libre.

    La app de escritorio de ChatGPT abre `thread-writer-locks/<uuid>.lock` en escritura
    mientras el hilo esta cargado, y con ese lock puesto `codex exec resume` falla SIEMPRE
    con "already has an active writer". No es una carrera: mientras la app este abierta el
    motor codex no puede escribir en su propio hilo.

    Se mira /proc en vez de llamar a lsof para no depender de un binario externo.
    """
    lock = CODEX_LOCKS / f"{thread}.lock"
    if not lock.is_file():
        return 0
    target = str(lock.resolve())
    mio = os.getpid()
    for proc in os.listdir("/proc"):
        if not proc.isdigit() or int(proc) == mio:
            continue
        fds = f"/proc/{proc}/fd"
        try:
            nombres = os.listdir(fds)
        except OSError:
            continue
        for fd in nombres:
            try:
                # readlink y no resolve(): es una syscall en vez de recorrer el arbol
                if os.readlink(f"{fds}/{fd}") == target:
                    return int(proc)
            except OSError:
                continue
    return 0


def _proceso_nombre(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
    except OSError:
        return "?"


def _codex_command(prompt: str) -> tuple[list[str] | None, str, str]:
    thread, automation_status = _automation_config()
    if not thread:
        return None, "", "falta target_thread_id o WSP_CODEX_THREAD_ID"
    if os.environ.get("WSP_ALLOW_ACTIVE_AUTOMATION") != "1":
        conflicts = _active_automations_on_thread(thread)
        if automation_status.upper() == "ACTIVE" and not conflicts:
            conflicts = ["wspbot"]
        if conflicts:
            return None, "", (
                "hay automations ACTIVE sobre el mismo hilo: "
                + ", ".join(conflicts)
                + "; pausalas para evitar ejecuciones paralelas"
            )
    if not Path(CODEX).is_file():
        return None, "", f"no existe Codex CLI: {CODEX}"
    return [
        CODEX,
        "exec",
        "--model",
        CODEX_MODEL,
        "--config",
        f'model_reasoning_effort="{CODEX_EFFORT}"',
        "resume",
        "--all",
        thread,
        prompt,
    ], thread, ""


def _explicar_fallo(rc: int) -> str:
    """Traduce el error de codex cuando el hilo lo tiene tomado la app de ChatGPT.

    El chequeo se hace DESPUES y leyendo el log, no antes: mirar /proc entero para saber
    quien tiene el lock cuesta cientos de milisegundos y no vale la pena pagarlos en cada
    vuelta para un caso que el propio codex ya detecta.
    """
    motor = _agente()
    base = f"{motor} termino con {rc}"
    if motor != "codex":
        return base
    try:
        with RUN_LOG.open("rb") as log:
            log.seek(0, os.SEEK_END)
            log.seek(max(0, log.tell() - 4096))
            cola = log.read().decode("utf-8", "replace")
    except OSError:
        return base
    if "already has an active writer" not in cola:
        return base
    thread, _ = _automation_config()
    writer = _thread_writer_pid(thread) if thread else 0
    quien = f"{_proceso_nombre(writer)} (PID {writer})" if writer else "la app de ChatGPT"
    return (
        f"{base}: el hilo lo tiene tomado {quien}. "
        "Cierra la app de ChatGPT o cambia a WSP_AGENT=claude"
    )


def _run_agent(prompt: str = PROMPT) -> tuple[bool, str]:
    motor = _agente()
    if motor == "claude":
        cmd, session, error = _claude_command(prompt)
    elif motor == "codex":
        cmd, session, error = _codex_command(prompt)
    else:
        return False, f"WSP_AGENT no reconocido: {motor!r} (usa 'codex' o 'claude')"
    if cmd is None:
        return False, error

    kind = "compactar" if prompt == COMPACT_PROMPT else "vuelta"
    _append_log(f"RUN agent={motor} sesion={session} prompt={kind}")
    with RUN_LOG.open("a", encoding="utf-8") as log:
        result = subprocess.run(
            cmd,
            cwd=BASE,
            env=_entorno_limpio(),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
    if result.returncode != 0:
        return False, _explicar_fallo(result.returncode)
    return True, ""


def _quiet_mute_batch(count: int, has_ginger: bool) -> tuple[bool, str]:
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

    cmd = [REGISTRAR]
    if has_ginger:
        cmd += ["--ts", "visto"]
    cmd += ["--log", f"MUDO — {count} mensajes nuevos"]
    reg = subprocess.run(cmd, cwd=BASE, text=True, capture_output=True, check=False)
    if reg.returncode != 0:
        return False, f"registrar MUDO termino con {reg.returncode}: {reg.stderr.strip()}"
    _append_log(f"MUDO local count={count} sin modelo")
    return True, ""


def _quiet_mikel_batch(count: int) -> tuple[bool, str]:
    """Cierra texto de Mikel que no iba dirigido a Tulpa sin gastar una vuelta."""
    try:
        pulso = subprocess.run(
            [PULSO], cwd=BASE, text=True, capture_output=True, check=False
        )
    except OSError as exc:
        return False, f"no se pudo correr pulso local: {exc}"
    if pulso.returncode != 0:
        return False, f"pulso local termino con {pulso.returncode}: {pulso.stderr.strip()}"
    if "ESTADO: BRIDGE_CAIDO" in pulso.stdout:
        return False, "bridge caido durante la vuelta local"

    reg = subprocess.run(
        [REGISTRAR, "--log", f"NO ME METI — {count} mensajes tuyos no dirigidos (local)"],
        cwd=BASE,
        text=True,
        capture_output=True,
        check=False,
    )
    if reg.returncode != 0:
        return False, f"registrar local termino con {reg.returncode}: {reg.stderr.strip()}"
    _append_log(f"MIKEL local count={count} sin modelo")
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


def _requeue(reasons: list[str]) -> None:
    """Devuelve las razones a la cola para que un fallo no pierda la vuelta."""
    with _locked(STATE_LOCK):
        state = _load_state()
        state["pending_reasons"] = sorted(set(state.get("pending_reasons") or []) | set(reasons))
        _save_state(state)


def _compact_thread() -> tuple[bool, str]:
    """Corre ``/compactar`` sobre el mismo hilo, dentro de este lock.

    Antes lo hacia la automation ``wspbot-2`` cada hora. Su scheduler no ve
    ``worker.lock``, asi que podia lanzar Codex encima de una vuelta a medias. Aqui la
    compactacion espera su turno y el reloj se guarda aunque falle, para no reintentar en
    bucle contra un hilo roto.
    """
    ok, error = _run_agent(COMPACT_PROMPT)
    with _locked(STATE_LOCK):
        state = _load_state()
        state["last_compact_at"] = time.time()
        if ok:
            state["compactions"] = int(state.get("compactions") or 0) + 1
            state["last_error"] = ""
        _save_state(state)
    return ok, error


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

            new_messages = _new_message_count()
            event_actionable = "message" in reasons and new_messages > 0
            timed_actionable = "mute_expired" in reasons and MUDO.exists()
            silence_actionable = "silence" in reasons and not MUDO.exists() and _silence_due()
            # Se descarta la compactacion si el motor de AHORA no la necesita. El
            # `compact` que dejo encolado un watcher en codex no puede acabar
            # corriendose como `claude -p "/compactar"`, que ahi no es ningun comando:
            # seria una vuelta entera gastada en mandarle una barra al modelo.
            compact_pending = (
                "compact" in reasons
                and _compacta_a_mano()
                and _compact_min() > 0
                and not MUDO.exists()
            )
            if not (event_actionable or timed_actionable or silence_actionable):
                if compact_pending:
                    # Compactar no toca el chat ni depende de que haya mensajes: es
                    # mantenimiento del hilo. Se corre igual y la vuelta acaba aqui.
                    ok, error = _compact_thread()
                    if not ok:
                        _requeue(reasons)
                        _finish_worker(error)
                        _append_log(f"ERROR {error}")
                        return 1
                    continue
                with _locked(STATE_LOCK):
                    state = _load_state()
                    state["suppressed_empty"] = int(state.get("suppressed_empty") or 0) + 1
                    _save_state(state)
                _append_log(f"SKIP vacio reasons={','.join(reasons)}")
                continue

            if MUDO.exists() and event_actionable and not _new_exact_on():
                _, deadline = _mute_deadline()
                if not deadline or deadline > time.time():
                    ok, error = _quiet_mute_batch(
                        new_messages,
                        any(not from_me for _, from_me, _ in _new_messages()),
                    )
                    if ok:
                        with _locked(STATE_LOCK):
                            state = _load_state()
                            state["quiet_mute_batches"] = int(state.get("quiet_mute_batches") or 0) + 1
                            state["last_error"] = ""
                            _save_state(state)
                        if compact_pending:
                            ok, error = _compact_thread()
                            if not ok:
                                _requeue(["compact"])
                                _finish_worker(error)
                                _append_log(f"ERROR {error}")
                                return 1
                        continue
                    _requeue(reasons)
                    _finish_worker(error)
                    _append_log(f"ERROR {error}")
                    return 1

            if event_actionable and not MUDO.exists() and not _message_batch_needs_agent():
                ok, error = _quiet_mikel_batch(new_messages)
                if ok:
                    with _locked(STATE_LOCK):
                        state = _load_state()
                        state["quiet_mikel_batches"] = int(
                            state.get("quiet_mikel_batches") or 0
                        ) + 1
                        state["last_error"] = ""
                        _save_state(state)
                    continue
                _requeue(reasons)
                _finish_worker(error)
                _append_log(f"ERROR {error}")
                return 1

            ok, error = _run_agent()
            if not ok:
                _requeue(reasons)
                _finish_worker(error)
                _append_log(f"ERROR {error}")
                return 1
            with _locked(STATE_LOCK):
                state = _load_state()
                state["runs"] = int(state.get("runs") or 0) + 1
                state["last_run_at"] = time.time()
                state["last_error"] = ""
                _save_state(state)

            # La compactacion va DESPUES de la vuelta: primero se le contesta a Contacto y
            # solo entonces se recorta el hilo, nunca al reves.
            # La vuelta pudo recibir `/off`: se vuelve a mirar el archivo para no gastar
            # una compactacion justo despues de quedar MUDO.
            if compact_pending and not MUDO.exists():
                ok, error = _compact_thread()
                if not ok:
                    _requeue(["compact"])
                    _finish_worker(error)
                    _append_log(f"ERROR {error}")
                    return 1


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


def _compact_due() -> bool:
    """El reloj arranca la primera vez que se mira, no en 1970.

    Sin esto un estado recien creado compactaria de entrada, que es justo lo contrario de
    lo que se quiere: el hilo acaba de empezar y no hay nada que recortar.
    """
    if _compact_min() <= 0 or MUDO.exists():
        return False
    now = time.time()
    with _locked(STATE_LOCK):
        state = _load_state()
        last = float(state.get("last_compact_at") or 0.0)
        last_run = float(state.get("last_run_at") or 0.0)
        if not last:
            state["last_compact_at"] = now
            _save_state(state)
            return False
        # El reloj por si solo no crea contexto. Sin una vuelta real desde la ultima
        # compactacion no hay nada nuevo que recortar.
        if last_run <= last:
            return False
        # Ya encolada y todavia sin correr: el watcher mira cada 5 segundos y reencolar
        # en bucle solo reescribiria el estado sin cambiar nada.
        if "compact" in (state.get("pending_reasons") or []):
            return False
    return now - last >= _compact_min() * 60


def _down_for() -> float:
    """Segundos que lleva caido, o 0 si esta vivo. Lo pone _note_bridge_state."""
    since = float(_load_state().get("bridge_down_since") or 0.0)
    return time.time() - since if since else 0.0


def _rescatar_cola() -> None:
    """Vuelve a arrancar el worker si quedo trabajo en la cola sin nadie que lo tome.

    Cuando una vuelta falla las razones se reencolan, pero el worker sale y hasta ahora
    nadie las recogia: se quedaban esperando al siguiente mensaje de Contacto. Con un fallo
    transitorio eso deja al bot mudo indefinidamente — y hay uno que pasa de verdad: la
    app de ChatGPT toma el writer del hilo de Codex durante minutos y cada `exec resume`
    de esa ventana revienta.
    """
    ahora = time.time()
    arrancar = False
    with _locked(STATE_LOCK):
        state = _load_state()
        if not state.get("pending_reasons"):
            return
        # worker_pid None es la ventana entre reservar el worker y publicar su pid: ahi
        # hay uno arrancando y meter otro seria duplicar la vuelta.
        if state.get("worker_active") and (
            state.get("worker_pid") is None or _pid_alive(state.get("worker_pid"))
        ):
            return
        if ahora - float(state.get("last_retry_at") or 0.0) < REINTENTO:
            return
        state["last_retry_at"] = ahora
        state["retries"] = int(state.get("retries") or 0) + 1
        state["worker_active"] = True
        state["worker_pid"] = None
        _save_state(state)
        arrancar = True

    if not arrancar:
        return
    _append_log("REINTENTO cola pendiente sin worker")
    try:
        _spawn_worker()
    except RuntimeError as exc:
        if str(exc) == "foreground":
            worker()
            return
        raise
    except Exception as exc:
        with _locked(STATE_LOCK):
            state = _load_state()
            state["worker_active"] = False
            state["worker_pid"] = None
            state["last_error"] = f"no se pudo reintentar: {exc}"
            _save_state(state)
        _append_log(f"ERROR no se pudo reintentar: {exc}")


def _note_bridge_state(alive: bool) -> None:
    with _locked(STATE_LOCK):
        state = _load_state()
        current = float(state.get("bridge_down_since") or 0.0)
        if alive and current:
            state["bridge_down_since"] = 0.0
            _save_state(state)
        elif not alive and not current:
            state["bridge_down_since"] = time.time()
            _save_state(state)


def bridge_starting() -> int:
    """Abre una gracia nueva para que una caida vieja no avise durante el arranque."""
    with _locked(STATE_LOCK):
        state = _load_state()
        state["bridge_down_since"] = time.time()
        _save_state(state)
    try:
        MARCA_AVISO.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"ERROR: no se pudo limpiar el aviso anterior: {exc}", file=sys.stderr)
        return 1
    return 0


def watch() -> int:
    _ensure_state_dir()
    try:
        lock = _locked(WATCH_LOCK, blocking=False)
        lock.__enter__()
    except BlockingIOError:
        return 0
    try:
        _append_log(f"WATCH pid={os.getpid()} parent={PARENT_PID or '-'}")
        next_health = 0.0
        mtime = _mtime_script()
        while _parent_alive():
            # Antes que nada: si el script cambio, este proceso se reemplaza por el nuevo.
            # El flock se suelta solo, porque Python abre el lock con O_CLOEXEC.
            mtime = _recargar_si_cambio(mtime)
            # La salud del bridge se mira aparte y mas despacio que el resto: es la unica
            # comprobacion que sale del proceso. Sin heartbeat es tambien la unica que
            # queda avisando de una caida, porque un bridge muerto no manda eventos.
            if time.time() >= next_health:
                alive, reason = _bridge_health()
                _note_bridge_state(alive)
                next_health = time.time() + max(HEALTH_INTERVAL, 1.0)
                if not alive and _down_for() >= GRACIA_CAIDA:
                    _notify_bridge_down(reason)

            # Con el bridge caido la base esta congelada: el silencio que se lea aqui no
            # es silencio real y despertar al modelo solo gastaria tokens para nada.
            if not float(_load_state().get("bridge_down_since") or 0.0):
                if MUDO.exists():
                    raw, deadline = _mute_deadline()
                    if raw and deadline and deadline <= time.time():
                        _mark_and_queue("mute_expired", "mute_woken_for", raw)
                else:
                    latest = _latest_chat_timestamp()
                    if latest and _silence_due():
                        _mark_and_queue("silence", "silence_woken_for", latest)
                if _compact_due():
                    queue("compact")
                _rescatar_cola()
            time.sleep(max(WATCH_INTERVAL, 0.1))
        return 0
    finally:
        lock.__exit__(None, None, None)


def notify(args: argparse.Namespace) -> int:
    if args.chat_jid != JID:
        return 0
    return 0 if queue("message", args.message_id, args.direct) else 1


def doctor() -> int:
    problems = []
    if not DB.is_file():
        problems.append(f"falta DB: {DB}")

    motor = _agente()
    print(f"AGENT={motor}")
    print(f"JID={JID}")

    if motor == "claude":
        try:
            session = CLAUDE_SESSION.read_text(encoding="utf-8").strip()
        except OSError:
            session = ""
        if not Path(CLAUDE).is_file():
            problems.append(f"falta Claude Code: {CLAUDE}")
        print(f"CLAUDE={CLAUDE}")
        print(f"SESION={session or '(se crea en la primera vuelta)'}")
        print(f"MODELO={CLAUDE_MODEL or '(el de tu config)'}")
        print(f"FLAGS={' '.join(CLAUDE_FLAGS) or '-'}")
    elif motor == "codex":
        thread, status = _automation_config()
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
        writer = _thread_writer_pid(thread) if thread else 0
        if writer:
            problems.append(
                f"el hilo lo tiene abierto {_proceso_nombre(writer)} (PID {writer}); "
                "con la app de ChatGPT abierta `codex exec resume` falla siempre. "
                "Cierrala o usa el motor claude"
            )
        print(f"THREAD={thread or '-'}")
        print(f"MODELO={CODEX_MODEL}")
        print(f"ESFUERZO={CODEX_EFFORT}")
        print(f"AUTOMATION={status or '-'}")
        print(f"ACTIVE_EN_HILO={', '.join(conflicts) if conflicts else '-'}")
        print(f"WRITER={f'{_proceso_nombre(writer)} PID {writer}' if writer else 'libre'}")
    else:
        problems.append(f"WSP_AGENT no reconocido: {motor!r} (usa 'codex' o 'claude')")

    print(f"DEBOUNCE={DEBOUNCE:g}s")
    print(f"SILENCE={SILENCE_MIN}m")
    cada = _compact_min()
    print(f"COMPACTAR={f'cada {cada:g}m' if cada > 0 else 'no (automatico)'}")
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
    notify_parser.add_argument("--direct", action="store_true")
    sub.add_parser("worker", help="procesar la cola")
    sub.add_parser("watch", help="vigilar vencimientos de mudo y silencio")
    sub.add_parser("bridge-starting", help="abrir la gracia de arranque del bridge")
    sub.add_parser("doctor", help="validar configuracion sin despertar el modelo")
    sub.add_parser("status", help="mostrar estado de la cola")
    args = parser.parse_args()
    if args.command == "notify":
        return notify(args)
    if args.command == "worker":
        return worker()
    if args.command == "watch":
        return watch()
    if args.command == "bridge-starting":
        return bridge_starting()
    if args.command == "doctor":
        return doctor()
    return status()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda _signum, _frame: sys.exit(0))
    raise SystemExit(main())
