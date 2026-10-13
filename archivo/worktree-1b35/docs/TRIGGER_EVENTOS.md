# Trigger local por eventos

El heartbeat de un minuto queda reemplazado por dos piezas locales:

1. El bridge llama `bin/evento-wsp.py notify` despues de guardar por primera vez un
   mensaje del chat objetivo con `is_from_me = false`
2. Un watcher local cubre los dos eventos que no traen mensaje: vencimiento de `MUDO`
   con `HASTA:` y una sola consideracion al llegar al umbral de silencio

Ninguna de las dos piezas llama al modelo si el cursor demuestra que el evento ya fue
consumido. Los `HistorySync` no disparan nada

## Cola y concurrencia

`notify` deja una bandera persistente en `nucleo/eventos/state.json` y sale. El worker:

- espera 6 segundos desde el ultimo mensaje para agrupar la rafaga
- usa `flock` para que solo exista una ejecucion de Codex
- limpia la bandera justo antes de ejecutar
- si llega algo mientras trabaja la vuelve a ver al terminar y conserva una vuelta
  pendiente
- vuelve a consultar `LAST_VISTO_TS` antes de esa vuelta. Si la ejecucion anterior ya
  alcanzo el mensaje no despierta al modelo otra vez

El hilo no se recrea. Se reanuda el `target_thread_id` de la automation existente con:

```text
/usr/lib/chatgpt/resources/codex exec resume --all <thread_id> \
  "sigue al pie de la letra /home/kutex/WSP Bot/PROMPT.md"
```

Toda automation que apunte a ese mismo `target_thread_id` debe quedar `PAUSED`. El
coordinador escanea `~/.codex/automations/*/automation.toml` y se niega a correr si
encuentra alguna `ACTIVE`, porque su lock no puede impedir que el scheduler de la app
ejecute el mismo hilo por fuera. No basta con mirar `wspbot`: `wspbot-2` corre
`/compactar` cada hora sobre el mismo hilo y tambien colisiona. La vuelta no se pierde,
queda pendiente y `status` muestra el nombre de la automation que estorba

## MUDO y silencio

Mientras existe un `MUDO` valido y no entro un `/on` exacto el modelo no se despierta
El coordinador corre localmente `pulso.sh` y `registrar.sh --ts visto`. Asi conserva los
cursores y el log sin mandar mensajes ni checks azules

El watcher revisa solo archivos y SQLite cada 5 segundos. Eso no usa tokens. Cuando vence
un `HASTA:` encola una vuelta para que `pulso.sh` quite `MUDO` y se mantenga el saludo de
regreso. Para el silencio encola como maximo una vuelta por timestamp del ultimo mensaje
Si el modelo decide no sacar tema no vuelve a preguntarle cada minuto sobre el mismo
silencio

## Activacion

Despues de compilar y reiniciar el bridge:

```bash
cd "/home/kutex/WSP Bot/whatsapp-mcp/whatsapp-bridge"
go build -o whatsapp-client .
"/home/kutex/WSP Bot/bin/evento-wsp.py" doctor
```

`doctor` falla mientras quede una automation `ACTIVE` sobre el hilo. Su linea
`ACTIVE_EN_HILO=` las lista por nombre

El bridge arranca el watcher. Para desactivar el trigger sin recompilar:

```bash
WSP_EVENT_TRIGGER= ./whatsapp-client
```

Estado y diagnostico:

```bash
"/home/kutex/WSP Bot/bin/evento-wsp.py" status
tail -f "/home/kutex/WSP Bot/nucleo/eventos/runner.log"
```

## Ahorro

El heartbeat generaba 1440 oportunidades de arrancar el modelo por dia aunque WhatsApp
no tuviera nada nuevo. Con eventos el numero pasa a:

```text
rafagas entrantes + vencimientos MUDO + episodios de silencio
```

Por ejemplo 50 rafagas en un dia implican aproximadamente 50 vueltas por mensajes en vez
de 1440 checks programados: 96.5% menos arranques antes de contar los pocos eventos de
silencio. El ahorro real se mide con `runs` y `quiet_mute_batches` de `status`. No se
asigna una cifra de tokens fija porque cambia con el contexto y los hooks del hilo

## Riesgos conocidos

- Hace falta recompilar y reiniciar el bridge. El proceso viejo no emite eventos
- El filtro estricto ignora mensajes `is_from_me = true`. Un `/on` escrito por Mikel desde
  su propio telefono no despierta por si solo; uno entrante si
- Una ejecucion manual simultanea sobre el mismo hilo queda fuera del lock local y puede
  hacer fallar `codex exec resume`. La vuelta queda pendiente y `status` muestra el error.
  Las automations si estan cubiertas: el coordinador las revisa todas antes de arrancar
- Si el bridge esta caido no llegan eventos. Tampoco se finge que no hay mensajes
- El watcher hace una consulta SQLite local cada 5 segundos. Ahorra modelo y tokens pero
  no es un sistema completamente sin polling local
