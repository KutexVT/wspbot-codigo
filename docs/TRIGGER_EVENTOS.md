# Trigger local por eventos

El heartbeat de un minuto queda reemplazado por dos piezas locales:

1. El bridge llama `bin/evento-wsp.py notify` despues de guardar por primera vez un
   mensaje del chat objetivo, venga de quien venga. Lo de Contacto y lo que Mikel escribe
   desde su telefono despiertan igual; lo unico excluido es lo que manda la propia tulpa
   por `/api/send`
2. Un watcher local cubre lo que no llega como mensaje: vencimiento de `MUDO` con
   `HASTA:`, una sola consideracion al llegar al umbral de silencio, la compactacion del
   hilo y el aviso de escritorio cuando el bridge se cae

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
- si algo falla devuelve las razones a la cola, para que un error no se coma la vuelta

El hilo no se recrea. Se reanuda el `target_thread_id` de la automation existente con:

```text
/usr/lib/chatgpt/resources/codex exec resume --all <thread_id> \
  "sigue al pie de la letra /home/kutex/WSP Bot/PROMPT.md"
```

### Arrancar sin IA o elegir motor

Sin flag arranca únicamente el bridge. Los flags activan el coordinador y guardan el
motor elegido:

```bash
wspbot             # solo bridge, sin IA
wspbot --claude    # Claude Code
wspbot --codex     # hilo de la app de ChatGPT
wspbot --stop-ia   # apaga un watcher inactivo antes de cambiar de modo
wspbot --status    # salud del bridge y estado de la IA
```

`wspbot` exporta `WSP_EVENT_TRIGGER=""`, por lo que el bridge no emite eventos ni arranca
el watcher. También omite `doctor` y `bridge-starting`: esas comprobaciones pertenecen al
coordinador y no hacen falta para usar WhatsApp como bridge normal

Los flags escriben el motor en `nucleo/eventos/agente`. `evento-wsp.py` lo lee con esta
precedencia:

1. `WSP_AGENT` del entorno — gana siempre, es la valvula de escape para probar
2. `nucleo/eventos/agente` — lo que eligio el ultimo flag
3. `codex`

Guardarlo en disco es lo que hace que `doctor` diga la verdad desde cualquier terminal.
Leyendolo solo del entorno, una shell sin el export contestaba `codex` mientras el bridge
corria con Claude, y no habia forma de notarlo

Los modos son exclusivos. Si hay otro bridge, una vuelta o un watcher, el comando falla
antes de cambiar `nucleo/eventos/agente`. Primero se cierra el bridge con `Ctrl+C`; una
vuelta activa se deja terminar; y un watcher inactivo se apaga con `wspbot --stop-ia`

`wspbot` es un script (`bin/wspbot`) y no el alias de antes, porque un alias no puede
llevar flags: `wspbot --claude` le habria pasado el `--claude` al binario Go

Y eso paso: una zsh abierta antes del cambio conserva el alias viejo en memoria, asi que
`wspbot --claude` arranco el bridge con el motor guardado todavia en `codex` y las cuatro
vueltas siguientes murieron contra el writer de la app de ChatGPT sin decir por que. Por
eso el binario ahora **rechaza cualquier argumento** en vez de tragarselo:

```text
whatsapp-client no acepta argumentos: [--claude]
los flags del motor son de wspbot:  wspbot --claude | --codex
si acabas de cambiar el alias, abre una terminal nueva o:  unalias wspbot
```

Con `claude` el comando pasa a ser:

```text
claude -p --dangerously-skip-permissions --resume <uuid> \
  "sigue al pie de la letra /home/kutex/WSP Bot/PROMPT.md"
```

La vuelta corre con el entorno limpio de marcas de sesion (`CLAUDECODE`,
`CLAUDE_CODE_SESSION_ID` y demas). El watcher hereda el entorno de quien lo lanzo y
despues vive horas: si eso fue una sesion de Claude Code, sus variables viajaban pegadas
hasta el `claude -p --resume` de la vuelta, que es otra sesion distinta

El uuid se fija en la primera vuelta con `--session-id` y se guarda en
`nucleo/eventos/claude-session`. A partir de ahi todas las vueltas lo reanudan, asi que el
bot conserva su contexto entre mensajes. **Borrar ese archivo es empezar de cero** y se
pierde todo lo acumulado

Los flags salen de `WSP_CLAUDE_FLAGS` (por defecto
`--dangerously-skip-permissions`). Sin eso la vuelta se cuelga esperando una confirmacion
que nadie va a dar: no hay terminal al otro lado. El modelo se fija con `WSP_CLAUDE_MODEL`
y si va vacio usa el de tu config

El hook de estilo corre con los dos motores: `~/.claude/hooks/estilo-kutex.sh` desde
`settings.json` y la misma copia en `~/.codex/hooks/estilo-kutex.sh` desde
`~/.codex/hooks.json`. Cambiar de motor no deja al bot escribiendo sin el perfil delante

Con `claude` la compactacion manual queda desactivada, porque Claude Code recorta la
ventana solo. Y las automations de Codex dejan de mirarse: no hay hilo suyo que proteger

**Los dos motores no comparten contexto.** Cambiar de uno a otro arranca con la memoria
en blanco; el hilo de Codex no se migra

### Automations sobre el mismo hilo

Toda automation que apunte a ese mismo `target_thread_id` debe quedar `PAUSED`. El
coordinador escanea `~/.codex/automations/*/automation.toml` entero y se niega a correr si
encuentra alguna `ACTIVE`, porque su lock no puede impedir que el scheduler de la app
ejecute el mismo hilo por fuera

No basta con mirar `wspbot`: `wspbot-2` corria `/compactar` cada hora sobre el mismo hilo
y colisionaba igual. La vuelta no se pierde, queda pendiente y `status` muestra el nombre
de la automation que estorba

## MUDO y silencio

Mientras existe un `MUDO` valido y no entro un `/on` exacto el modelo no se despierta
El coordinador corre localmente `pulso.sh` y `registrar.sh --ts visto`. Asi conserva los
cursores y el log sin mandar mensajes ni checks azules

El watcher revisa archivos y SQLite cada 5 segundos. Eso no usa tokens. Cuando vence un
`HASTA:` encola una vuelta para que `pulso.sh` quite `MUDO` y se mantenga el saludo de
regreso. Para el silencio encola como maximo una vuelta por timestamp del ultimo mensaje
Si el modelo decide no sacar tema no vuelve a preguntarle cada minuto sobre el mismo
silencio

## Salud del bridge

Esto es lo que el heartbeat hacia sin que se notara: `pulso.sh` manda un `notify-send`
critico cuando el bridge no contesta, y lo repite cada 15 minutos. Por eventos ese aviso
no puede salir solo — un bridge muerto no genera eventos, y sin eventos no hay vuelta que
corra `pulso.sh`. Justo cuando mas hace falta, se apagaria

Por eso el watcher **no es hijo del bridge**. El bridge lo arranca con `setsid`, o sea en
su propia sesion, y sigue vivo cuando el bridge muere. Su `watch.lock` impide que un
reinicio levante un segundo. El watcher:

- consulta `http://127.0.0.1:8080/api/health` cada 60 segundos, con los mismos casos que
  `salud_bridge` de `pulso.sh` (un 404 es el binario viejo, que esta vivo igual)
- manda el mismo aviso critico, respetando `nucleo/.aviso_bridge` para no duplicarlo con
  el que manda `pulso.sh`
- **espera 45 segundos antes del primer aviso** (`WSP_EVENT_GRACIA`). El bridge lo lanza
  antes de levantar el HTTP, asi que la primera consulta siempre falla: sin esa gracia
  cada `wspbot` disparaba una notificacion critica de mentira
- **no encola nada** mientras el bridge este caido. La base esta congelada: el silencio
  que se lea ahi no es silencio real y despertar al modelo seria gastar tokens en una
  mentira

### Se recarga solo

El watcher sobrevive al bridge, asi que `wspbot` NO lo reinicia y durante un tiempo eso
significo que un arreglo escrito en `evento-wsp.py` no llegaba a correr nunca: el proceso
vivo seguia con el codigo de cuando arranco. Paso de verdad el 2026-08-25 — el rescate de
cola estaba escrito y el watcher de las 11:16 no lo tenia, con una vuelta parada 24
minutos y Mikel contestandole a Contacto a mano

Ahora el watcher compara el mtime del script en cada ciclo y, si cambio, se reemplaza con
`os.execv`. Conserva el PID, y el `watch.lock` se suelta solo porque Python abre el lock
con `O_CLOEXEC`. Antes de saltar compila el archivo: un guardado a medias mataria al unico
proceso que avisa de que el bridge se cayo, asi que si no compila se queda con el codigo
viejo y lo dice una vez en `runner.log`

Para matarlo a mano:

```bash
pkill -f "evento-wsp.py watch"
```

## Compactacion del hilo

La hacia `wspbot-2` cada hora desde el scheduler de la app, fuera del lock. Ahora la
encola el watcher y la corre el worker, con estas reglas:

- el reloj arranca la primera vez que se mira y no en 1970, para que un estado recien
  creado no compacte de entrada
- va **despues** de la vuelta, nunca antes: primero se le contesta a Contacto y solo
  entonces se recorta el hilo
- no cuenta como `run` ni escribe en el chat
- si falla se reencola, pero el reloj se guarda igual para no reintentar en bucle contra
  un hilo roto

Se ajusta con `WSP_COMPACT_CADA_MIN` (60 por defecto; `0` la desactiva)

Con el motor `claude` una compactacion que ya estuviera en la cola **se descarta** en vez
de correrse. Son dos cosas distintas y por eso van separadas: `WSP_COMPACT_CADA_MIN=0` es
"no la programes" y el motor es "no la entiende". Sin esto, el `compact` que dejo encolado
un watcher en codex se ejecutaba despues como `claude -p "/compactar"` — una vuelta entera
gastada en mandarle una barra al modelo

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

`doctor` valida el motor que este seleccionado y lo dice en su primera linea

Estado y diagnostico:

```bash
"/home/kutex/WSP Bot/bin/evento-wsp.py" status
tail -f "/home/kutex/WSP Bot/nucleo/eventos/runner.log"
```

## Ahorro

El heartbeat generaba 1440 oportunidades de arrancar el modelo por dia aunque WhatsApp
no tuviera nada nuevo. Con eventos el numero pasa a:

```text
rafagas entrantes + vencimientos MUDO + episodios de silencio + 24 compactaciones
```

Por ejemplo 50 rafagas en un dia implican aproximadamente 50 vueltas por mensajes en vez
de 1440 checks programados: 96.5% menos arranques antes de contar los pocos eventos de
silencio. Las 24 compactaciones no son ahorro nuevo, son las mismas que ya corria
`wspbot-2`; lo que cambia es que ahora esperan su turno en vez de pisar una vuelta

El ahorro real se mide con `runs`, `compactions` y `quiet_mute_batches` de `status`. No se
asigna una cifra de tokens fija porque cambia con el contexto y los hooks del hilo

## El writer del hilo de Codex

Con el motor `codex` hay un fallo que **no es una carrera ocasional**: la app de escritorio
de ChatGPT abre `~/.codex/thread-writer-locks/<uuid>.lock` en escritura mientras tiene el
hilo cargado, y en toda esa ventana `codex exec resume` falla:

```text
thread-store conflict: thread 01a037ad-... already has an active writer
```

Se vio en produccion el 2026-08-25: cuatro vueltas seguidas muertas entre las 11:24 y las
11:32, y el bot mudo hasta que la app solto el lock

Lo que hace el coordinador:

- **traduce el error**. `last_error` pasa de "codex termino con 1" a nombrar el proceso que
  tiene el hilo y decir que hacer. El escaneo de `/proc` se hace solo despues del fallo y
  en `doctor`, nunca antes de cada vuelta: mirar todos los fds cuesta cientos de
  milisegundos y no vale la pena pagarlos en el camino caliente
- **lo reintenta**. Ver la seccion de abajo
- `doctor` lo lista en su linea `WRITER=`

Con el motor `claude` esto no pasa: no hay ninguna app compitiendo por el mismo store

## Rescate de la cola

Cuando una vuelta falla las razones se reencolan, pero el worker sale. Hasta que se metio
el rescate nadie las recogia y se quedaban esperando el siguiente mensaje de Contacto — o
sea que un fallo transitorio dejaba al bot mudo indefinidamente

El watcher revisa la cola en cada ciclo y arranca el worker si hay trabajo sin dueño,
como maximo una vez cada `WSP_EVENT_REINTENTO` segundos (120 por defecto). Nunca duplica
un worker vivo. El contador va en `retries` de `status`

## Riesgos conocidos

- Hace falta recompilar y reiniciar el bridge. El proceso viejo no emite eventos
- Con el motor `codex`, tener la app de ChatGPT abierta bloquea el hilo. Ver arriba
- Cada mensaje de Mikel arranca una vuelta del modelo, aunque casi todas cierren sin
  escribir nada. Escribiendole a Contacto en vivo eso son bastantes vueltas mas al dia. La
  perilla es `WSP_EVENT_DEBOUNCE` (6s por defecto), que junta la rafaga en una sola vuelta
- Lo unico que impide que el bot se conteste a si mismo son `isNew` (`HasMessage`) y el
  `sync.Map` de `rememberSentByAPI`, que reclama el id en cuanto sale por `/api/send`.
  WhatsApp no devuelve el eco de lo que este mismo dispositivo mando, asi que en la
  practica no se llega ni al segundo freno — pero no se debilita ninguno de los dos
- Una ejecucion manual simultanea sobre el mismo hilo queda fuera del lock local y puede
  hacer fallar `codex exec resume`. La vuelta queda pendiente y `status` muestra el error.
  Las automations si estan cubiertas: el coordinador las revisa todas antes de arrancar
- Con el bridge caido no llegan eventos y el bot no trabaja. Eso no cambia. Lo que si se
  conserva es el aviso: el watcher sobrevive y avisa cada 15 minutos
- El watcher hace una consulta SQLite local cada 5 segundos y una peticion HTTP cada 60.
  Ahorra modelo y tokens pero no es un sistema completamente sin polling local
- Al sobrevivir al bridge, el watcher puede quedar huerfano si se desinstala el trigger.
  Se mata con `pkill -f "evento-wsp.py watch"`
- El motor se lee en caliente en cada uso (`_agente()`), nunca cacheado al importar. Antes
  era una constante de import y un watcher arrancado en `codex` seguia encolando
  `/compactar` despues de un `wspbot --claude`, que con ese motor no es ningun comando
