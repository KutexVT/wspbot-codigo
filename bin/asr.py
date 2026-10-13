#!/usr/bin/env python3
"""asr.py — transcribe un audio a texto. Lo llama oir.sh; no se usa a mano.

    asr.py <audio> --motor parakeet          # rapido, no alucina en silencios
    asr.py <audio> --motor whisper --modelo small
    asr.py <audio> --motor whisper --modelo large-v3-turbo

CONTRATO, y es lo importante de este archivo:

  - La transcripcion va a STDOUT y NADA MAS va a stdout. Avisos y errores, a stderr.
  - Si algo falla, se sale con codigo != 0 y stdout queda VACIO.

Eso segundo es la leccion del 2026-08-14: whisper-cli reventaba por un ggml roto y escupia
el backtrace de gdb por *stdout*, oir.sh lo capturaba tal cual y lo cacheaba como si fuera
lo que dijo ella. Un fallo ruidoso se volvio una cita inventada. Aqui cualquier excepcion
mata el proceso antes de imprimir una sola linea de texto.

El presupuesto de tiempo lo pone oir.sh con --presupuesto: se emiten segmentos hasta
agotarlo y se corta. No se trocea el audio a mano — los dos motores devuelven segmentos ya
cortados por pausas, asi que el corte cae entre frases y no parte palabras por la mitad
(que es lo que hacia el troceado por offset del oir.sh viejo).
"""

import argparse
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELOS = os.path.join(BASE, "bin", "models")

# Los pesos se quedan dentro del proyecto en vez de en ~/.cache, que es donde iria por
# defecto. bin/models/ ya esta en .gitignore y ahi vivia el ggml-small.bin de antes.
os.environ.setdefault("HF_HOME", MODELOS)
os.environ.setdefault("HF_HUB_CACHE", MODELOS)


def log(msg):
    """Todo lo que no sea la transcripcion va aqui."""
    print(msg, file=sys.stderr)


def duracion(ruta):
    """Segundos de audio, o 0 si no se puede saber. Solo sirve para el aviso de recorte."""
    try:
        import subprocess
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", ruta],
            capture_output=True, text=True, timeout=20,
        )
        return float(out.stdout.strip() or 0)
    except Exception:
        return 0.0


# Tope de cordura, no de rendimiento: un audio mas largo que esto se corta antes de
# transcribirlo. Sin el, una nota de voz absurda bloquea la vuelta del bot entera —
# parakeet va a ~7,7x tiempo real, asi que 30 min de audio son ~4 min de reloj.
MAX_SEG = 1800

# Cuantos segundos de audio procesa parakeet por segundo de reloj. Medido el 2026-08-16
# sobre 18 audios reales del chat en este Ryzen: 280 s de audio en 36 s = 7,7x. Se deja
# margen porque la maquina puede estar cargada.
RITMO_PARAKEET = 6.0


def a_wav(ruta, destino, tope=MAX_SEG):
    """opus de WhatsApp → wav 16 kHz mono, recortado al tope.

    faster-whisper lee ogg directo (usa PyAV), pero onnx-asr solo acepta RIFF/WAV: sin
    esto suelta 'file does not start with RIFF id'.
    """
    import subprocess
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", ruta, "-t", str(tope),
         "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", "-y", destino],
        check=True, capture_output=True, timeout=300,
    )
    return destino


def con_parakeet(ruta, presupuesto, idioma):
    """NVIDIA Parakeet TDT 0.6B v3 en ONNX int8.

    Su decoder transducer puede emitir un simbolo 'blank', asi que un silencio se
    transcribe como nada en vez de como texto inventado. Por eso no suelta los
    '[Musica] [Musica] [Musica]' que ensucian las transcripciones viejas de whisper.
    """
    import tempfile
    import onnx_asr

    # Sin path=: asi lo baja del hub la primera vez y despues lo reusa de HF_HUB_CACHE.
    # Pasandole path= se pone a buscarlo en local y falla si no esta ya descargado.
    modelo = onnx_asr.load_model("nemo-parakeet-tdt-0.6b-v3", quantization="int8")

    # onnx-asr no expone streaming por segmentos: es UNA sola pasada, no se puede parar a
    # mitad. Asi que el presupuesto no se aplica cortando la transcripcion sino recortando
    # el AUDIO antes de empezar: se calcula cuanto cabe con el ritmo medido del modelo.
    dur = duracion(ruta)
    cabe = min(MAX_SEG, presupuesto * RITMO_PARAKEET)
    recortar = dur > cabe
    tope = int(cabe) if recortar else MAX_SEG

    ini = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:
        texto = modelo.recognize(a_wav(ruta, os.path.join(tmp, "a.wav"), tope),
                                 language=idioma)
    gastado = time.monotonic() - ini
    if gastado > presupuesto:
        log(f"aviso: parakeet tardo {gastado:.0f}s, por encima del presupuesto de {presupuesto}s")
    return (texto or "").strip(), (tope if recortar else 0.0)


def con_whisper(ruta, presupuesto, idioma, modelo_id):
    """faster-whisper (CTranslate2, int8 en CPU).

    Se consumen segmentos hasta agotar el presupuesto y se corta. Devuelve tambien
    hasta que segundo del audio se llego, para que oir.sh pueda avisar del recorte.
    """
    from faster_whisper import WhisperModel

    modelo = WhisperModel(
        modelo_id,
        device="cpu",
        compute_type="int8",
        download_root=MODELOS,
    )

    segmentos, _info = modelo.transcribe(
        ruta,
        language=idioma,
        vad_filter=True,              # salta silencios: menos trabajo y menos alucinacion
        condition_on_previous_text=False,  # sin esto entra en bucle repitiendo la ultima frase
    )

    partes = []
    cubierto = 0.0
    ini = time.monotonic()
    for seg in segmentos:           # generador: se evalua perezosamente, se puede cortar
        partes.append(seg.text.strip())
        cubierto = seg.end
        if time.monotonic() - ini >= presupuesto:
            log(f"aviso: presupuesto de {presupuesto}s agotado, cortado en {cubierto:.0f}s de audio")
            break

    return " ".join(p for p in partes if p).strip(), cubierto


def main():
    p = argparse.ArgumentParser()
    p.add_argument("audio")
    p.add_argument("--motor", choices=["parakeet", "whisper"], default="parakeet")
    p.add_argument("--modelo", default="small", help="solo para whisper")
    p.add_argument("--idioma", default="es")
    p.add_argument("--presupuesto", type=float, default=90.0)
    args = p.parse_args()

    if not os.path.isfile(args.audio):
        log(f"ERROR: no existe el archivo: {args.audio}")
        return 1

    if args.motor == "parakeet":
        texto, cubierto = con_parakeet(args.audio, args.presupuesto, args.idioma)
    else:
        texto, cubierto = con_whisper(args.audio, args.presupuesto, args.idioma, args.modelo)

    if not texto:
        # Silencio de verdad. NO es un fallo: oir.sh lo distingue por el codigo 0.
        log("(sin habla reconocible)")
        return 0

    # El aviso de recorte va DELANTE del texto. Pegado al final se lee cuando ya te
    # formaste la idea de lo que dijo, o sea cuando ya no sirve de nada.
    dur = duracion(args.audio)
    if cubierto and dur and cubierto < dur - 5:
        texto = (f"[SOLO LOS PRIMEROS {int(cubierto // 60)} MIN de un audio de "
                 f"{int(dur // 60)}m{int(dur % 60)}s] {texto}")

    print(texto)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("interrumpido")
        sys.exit(130)
    except Exception as e:
        # Cualquier fallo sale por stderr y con codigo != 0, con stdout limpio.
        # Nunca se imprime nada que oir.sh pueda confundir con una transcripcion.
        log(f"ERROR ({type(e).__name__}): {e}")
        sys.exit(4)
