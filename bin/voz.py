#!/usr/bin/env python3
"""OmniVoice local. Solo prepara audio; responder.sh conserva el control del envío."""

import argparse
import contextlib
from difflib import SequenceMatcher
import json
import math
import os
from pathlib import Path
import random
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata

BASE = Path(__file__).resolve().parents[1]
CONFIG = BASE / "media/voz/config.json"


def hablado(texto):
    palabras = {
        "xq": "porque", "x": "por", "q": "que", "shi": "sí",
        "sip": "sí", "sipp": "sí", "xfis": "por favor",
        "encerio": "en serio", "osea": "o sea", "almenos": "al menos",
        "talvez": "tal vez", "aveces": "a veces", "masomenos": "más o menos",
    }
    texto = re.sub(r"\bntp+\b", "no te preocupes", texto, flags=re.I)
    return re.sub(r"\b\w+\b", lambda m: palabras.get(m[0].lower(), m[0]), texto)


def contenido_coincide(texto, transcripcion):
    def palabras(value):
        value = unicodedata.normalize("NFKD", hablado(value).casefold())
        return re.findall(r"\w+", "".join(c for c in value if not unicodedata.combining(c)))
    esperado, oido = palabras(texto), palabras(transcripcion)
    return (bool(esperado) and esperado[-2:] == oido[-2:]
            and SequenceMatcher(None, esperado, oido).ratio() >= 0.9)


def validar(config):
    if not isinstance(config, dict) or type(config.get("enabled")) is not bool:
        raise ValueError("config.json necesita enabled: true o false")
    if not isinstance(config.get("jid"), str) or "@" not in config["jid"]:
        raise ValueError("config.json necesita el jid autorizado")
    for key, default, low, high in [
        ("probability", 0.15, 0, 1), ("cooldown_seconds", 1800, 0, 86400),
        ("timeout_seconds", 90, 1, 300), ("steps", 32, 1, 64),
        ("threads", 6, 1, 16), ("speed", 1.0, 0.7, 1.4),
    ]:
        value = config.get(key, default)
        if (type(value) not in (int, float) or not math.isfinite(value)
                or not low <= value <= high):
            raise ValueError(f"{key} debe estar entre {low} y {high}")
        if key in ("steps", "threads") and int(value) != value:
            raise ValueError(f"{key} debe ser entero")
        config[key] = value
    return config


def elegible(config, texto, db, jid, ahora=None):
    if not config["enabled"] or config["jid"] != jid:
        return False
    if not 4 <= len(texto.split()) <= 18 or len(texto) > 160:
        return False
    if re.search(r"https?://|www\.|[`<>\[\]]|\n", texto, re.I):
        return False
    with contextlib.closing(sqlite3.connect(Path(db).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as conn:
        ultimo = conn.execute(
            "SELECT MAX(CAST(strftime('%s', timestamp) AS INTEGER)) FROM messages "
            "WHERE chat_jid=? AND sender='__tulpa__' AND media_type='audio'",
            (jid,),
        ).fetchone()[0]
    if ultimo is not None and (time.time() if ahora is None else ahora) - ultimo < config["cooldown_seconds"]:
        return False
    return random.random() < config["probability"]


def generar(config, texto, destino, carpeta):
    referencia = carpeta / config["reference_audio"]
    transcripcion = (carpeta / config["reference_text"]).read_text().strip()
    if not referencia.is_file() or not transcripcion:
        raise ValueError("falta la referencia de voz o su transcripción")
    os.environ.setdefault("HF_HOME", str(BASE / "bin/models/voz"))
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    import numpy as np
    import soundfile as sf
    import torch
    from omnivoice import OmniVoice

    # Reutilizar la conversión que ya usa el MCP para notas de voz.
    sys.path.insert(0, str(BASE / "whatsapp-mcp/whatsapp-mcp-server"))
    from audio import convert_to_opus_ogg

    ref, sr = sf.read(referencia, dtype="float32", always_2d=True)
    if not 3 <= len(ref) / sr <= 10 or not np.isfinite(ref).all():
        raise ValueError("la referencia debe durar entre 3 y 10 segundos")
    torch.set_num_threads(int(config["threads"]))
    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    dtype = torch.float16 if device.startswith("cuda") else torch.float32
    model = OmniVoice.from_pretrained("k2-fsa/OmniVoice", device_map=device, dtype=dtype)
    # ponytail: carga por audio; mantener el modelo residente solo si la latencia lo exige.
    with torch.inference_mode():
        audio = model.generate(
            text=hablado(texto), language="es",
            ref_audio=(torch.from_numpy(ref.mean(axis=1)), sr), ref_text=transcripcion,
            num_step=int(config["steps"]), speed=config["speed"],
        )[0]
    if (audio.ndim != 1 or not np.isfinite(audio).all()
            or not 0.4 <= len(audio) / model.sampling_rate <= 45
            or np.sqrt(np.mean(audio ** 2)) < 0.001):
        raise ValueError("OmniVoice devolvió audio vacío o inválido")
    destino.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destino.parent) as tmp:
        wav = Path(tmp) / "voz.wav"
        sf.write(wav, audio, model.sampling_rate)
        # Una variante probada perdía el final de la frase. Revisar el audio real
        # con el ASR existente antes de enviarlo y conservar lo que se oyó.
        transcripcion = subprocess.run(
            [str(BASE / "bin/venv-asr/bin/python"), str(BASE / "bin/asr.py"),
             str(wav), "--motor", "parakeet"],
            check=True, capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        if not contenido_coincide(texto, transcripcion):
            raise ValueError("la voz generada no coincide con el texto; audio rechazado")
        convert_to_opus_ogg(str(wav), str(destino))
        destino.with_suffix(".txt").write_text(transcripcion + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=os.environ.get("WSP_VOZ_CONFIG", str(CONFIG)))
    parser.add_argument("--modo", choices=("auto", "voz", "generar"), default="generar")
    parser.add_argument("--texto", required=True)
    parser.add_argument("--salida", type=Path)
    args = parser.parse_args()
    archivo = Path(args.config).resolve()
    if args.modo == "auto" and not archivo.exists():
        return
    config = validar(json.loads(archivo.read_text()))
    texto = args.texto.strip()
    if not texto or len(texto) > 600:
        raise ValueError("el texto debe tener entre 1 y 600 caracteres")
    if args.modo == "generar":
        if args.salida is None or args.salida.suffix.lower() != ".ogg":
            raise ValueError("--salida debe ser un archivo .ogg")
        with contextlib.redirect_stdout(sys.stderr):
            generar(config, texto, args.salida.resolve(), archivo.parent)
        print(args.salida.resolve())
        return

    jid = os.environ.get("WSP_JID", "000000000000000@lid")
    mudo = Path(os.environ.get("WSP_MUDO", str(BASE / "nucleo/MUDO")))
    if mudo.exists():
        raise ValueError("el bot está MUDO")
    if not config["enabled"] or config["jid"] != jid:
        if args.modo == "voz":
            raise ValueError("la voz está desactivada para este chat")
        return
    if args.modo == "auto" and not elegible(
        config, texto, os.environ.get("WSP_DB", str(BASE / "whatsapp-mcp/whatsapp-bridge/store/messages.db")), jid,
    ):
        return
    carpeta = archivo.parent / "generados"
    carpeta.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, nombre = tempfile.mkstemp(suffix=".ogg", prefix="voz-", dir=carpeta)
    os.close(fd)
    destino = Path(nombre)
    try:
        # El bot nunca descarga modelos durante una respuesta. Prepararlos con --modo generar.
        env = {**os.environ, "HF_HUB_OFFLINE": "1"}
        subprocess.run(
            [str(BASE / "bin/venv-voz/bin/python"), str(Path(__file__).resolve()),
             "--config", str(archivo), "--modo", "generar", "--texto", texto,
             "--salida", str(destino)],
            check=True, stdout=sys.stderr, env=env, timeout=config["timeout_seconds"],
        )
        if mudo.exists():
            raise ValueError("el bot está MUDO")
        print(destino)
    except BaseException:
        destino.unlink(missing_ok=True)
        destino.with_suffix(".txt").unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR VOZ: {exc}", file=sys.stderr)
        sys.exit(1)
