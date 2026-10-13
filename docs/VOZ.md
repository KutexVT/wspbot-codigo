# Notas de voz con OmniVoice

`bin/responder.sh` puede convertir algunas respuestas en notas de voz de WhatsApp con
una referencia de Mikel. Usa [OmniVoice 0.2.1](https://github.com/k2-fsa/OmniVoice) y el
envío OGG/Opus que ya tiene el bridge (`PTT=true`). No necesita reiniciar el bridge.

- Automático: 15% de las respuestas elegibles de 4 a 18 palabras y hasta 160 caracteres.
- Pausa: al menos 30 minutos desde el último audio de la Tulpa en ese chat.
- Enlaces y contenido con formato se mantienen como texto.
- `--voz` fuerza audio para una petición explícita y omite probabilidad y pausa.
- `--texto` fuerza texto. `--dry` valida y muestra la intención sin generar ni enviar.
- El ASR local revisa el audio generado. Si el texto no coincide o falta el final se rechaza.
- Un fallo de síntesis en automático permite enviar texto. Un fallo de HTTP nunca
  dispara otro envío: puede haber llegado el primero aunque no llegara su confirmación.
- MUDO se comprueba antes y después de generar y antes del HTTP. Las citas se conservan.

```bash
cd "/home/kutex/WSP Bot"
./bin/responder.sh "Oye no te preocupes que mañana te cuento bien"
./bin/responder.sh --voz --citar 3 "Oye no te preocupes"
./bin/responder.sh --texto "Oye"
./bin/responder.sh --dry --voz "Oye"
```

La configuración está en `media/voz/config.json`. `enabled` activa la conversión solo
para el `jid` indicado; no levanta MUDO. `probability`, `cooldown_seconds`, `steps`,
`threads`, `speed` y `timeout_seconds` permiten ajustar frecuencia y rendimiento.
La configuración local probada usa 16 pasos, 4 hilos de CPU y velocidad 0.9.
El límite de generación es de 90 segundos; los mensajes largos pueden superarlo.

La referencia activa es `referencia-natural.ogg`, seleccionada por Mikel tras escuchar
la comparación. `config-anterior.json` conserva el perfil inicial y `config-nueva.json`
el elegido; ambos son locales. La selección solo cambió la referencia y su transcripción.
Se comprobaron dos frases con distintas semillas: la nueva referencia obtuvo mayor
similitud automática con otros audios propios y mantuvo tiempos de generación similares.

`reference_audio` y `reference_text` apuntan a un audio de 3–10 segundos y su
transcripción literal. Las rutas relativas parten de la carpeta del JSON.
La referencia debe contener solo la voz de Mikel. Cambiarla cambia la voz sintetizada.
La similitud final debe juzgarse escuchando la muestra; no se garantiza una copia idéntica.

La referencia, su origen, la configuración y los audios quedan en `media/voz/`, excluido
de Git. El modelo queda en `bin/models/voz/` y el entorno en `bin/venv-voz/`, también
excluidos. La síntesis y el ASR son locales. Al responder se exige caché sin red;
los pesos se descargan durante la preparación inicial. Solo el audio final se envía
a WhatsApp cuando el bot está activo. Tras enviarlo, la transcripción reconocida se
guarda por ID en la caché existente para que pulso y el historial puedan leerlo.

## Recrear el entorno

```bash
uv venv --python 3.12 bin/venv-voz
uv pip install --python bin/venv-voz/bin/python \
  'torch==2.8.0+cpu' 'torchaudio==2.8.0+cpu' \
  --index-url https://download.pytorch.org/whl/cpu
uv pip install --python bin/venv-voz/bin/python \
  'omnivoice==0.2.1' 'transformers==5.16.1' \
  'torch==2.8.0+cpu' 'torchaudio==2.8.0+cpu' \
  --extra-index-url https://download.pytorch.org/whl/cpu \
  --index-strategy unsafe-best-match
```

Hace falta el entorno de ASR que ya usa `bin/oir.sh` y `ffmpeg`. Después de preparar
el JSON y la referencia, esta prueba descarga los pesos que falten y produce un audio
local incluso bajo MUDO. No envía mensajes:

```bash
bin/venv-voz/bin/python bin/voz.py --modo generar \
  --texto "Oye no te preocupes que mañana te cuento bien" \
  --salida media/voz/prueba.ogg
```

## Comprobaciones

```bash
bash -n bin/responder.sh
python3 -m unittest discover -s tests -p 'test_*.py' -q
git diff --check
```

`WSP_VOZ_CONFIG`, `WSP_DB`, `WSP_JID`, `WSP_MUDO`, `WSP_CITAS`,
`WSP_ENVIO_URL` y `WSP_TRANSCRIPCIONES` permiten una prueba aislada sin tocar el chat.
