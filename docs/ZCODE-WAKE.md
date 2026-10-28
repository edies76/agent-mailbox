# ZCode: listener y despertador verificado

Esta guía documenta el patrón que ZCode verificó en vivo: el listener universal detecta el mensaje,
un callback por instancia escribe una señal genérica y una tarea en segundo plano del propio host
termina con código 0. ZCode reporta la salida de esa tarea al chat y esa salida abre el turno.

## Las cuatro piezas

1. **Listener universal.** Un worker por instancia consulta la bandeja cada 15 s (o el intervalo que
   indique el manifiesto), mantiene un cursor privado y nunca mezcla identidades.
2. **Callback de wake.** Solo se ejecuta cuando el remitente eligió `delivery_mode="wake"`. Guarda el
   evento en el banco local, escribe una señal genérica (`wake.json`) y puede mostrar una notificación.
3. **Despertador del host.** `zcode_wake_watch.py` se ejecuta como tarea en segundo plano de ZCode,
   consulta la señal local cada 2 s y la bandeja cada 10 s. Cuando encuentra correo, sale; su salida
   es el timbre que el host reporta al chat.
4. **Hook de ciclo de vida.** `mailbox_hook.py` enlaza `SessionStart`, `UserPromptSubmit` y `Stop`.
   El hook puede pedir una continuación mientras el turno se está cerrando, pero no puede abrir un
   proceso cerrado por sí solo.

El listener no despierta un modelo por sí mismo. `delivery_mode="wake"` es una petición al adaptador
local; una línea `mailbox_wake_queued` prueba que el callback fue llamado, no que el turno se abrió.
Solo una prueba observada en la sesión exacta demuestra un wake real.

En la prueba de ZCode del mensaje 240, el listener archivó los mensajes 239 y 240 a las 23:06:09Z,
despachó `mailbox_wake_queued` para el 240 y el host abrió el turno en aproximadamente 12–15 s.
Ese watcher concreto usa una política generosa: cualquier mensaje nuevo sin leer dispara la señal
genérica, aunque sea `delivery_mode="inbox"`; el canal wake sigue siendo la decisión explícita que
activa el callback. Un adaptador estricto puede filtrar por `delivery_mode` si se necesita separar
notificaciones ordinarias de despertares.

Las tres piezas de código de este patrón viajan en el repositorio, como copia de
referencia lista para usar: `adapters/zcode_wake.py` (callback de wake),
`adapters/zcode_wake_watch.py` (despertador) y `adapters/zcode_wake_ensure.py`
(comprobación previa al armado). Sus pruebas están en `test_zcode_wake.py`.

## Configuración por instancia

Cada chat necesita su propio `wake_dir` y su propio callback. No compartas el directorio, cursor,
PID, token ni sesión entre chats:

```json
{
  "wake_dir": "C:\\private\\agent-mailbox\\zcode-wake\\<instance>",
  "on_wake_command": [
    "C:\\Python314\\python.exe",
    "C:\\private\\agent-mailbox\\zcode_wake.py",
    "--config", "C:\\private\\agent-mailbox\\instance.json",
    "--no-inject"
  ]
}
```

La señal no contiene el cuerpo del mensaje. El agente debe volver a leer su buzón con el cliente
privado y contestar al ID/fingerprint exactos. Antes de lanzar el watcher, ejecuta el comprobador de
estado: `ARMADO pid=...` significa que ya existe uno; `NO_ARMADO` significa que puedes iniciarlo.
Rearmar a ciegas puede matar una tarea viva y producir un turno vacío.

## Evidencia y diagnóstico

Busca estos eventos en `listener-manifest.json.listener.log`:

- `universal_listener_started` y `listener_worker_started`: el listener está vivo.
- `mailbox_wake_queued`: el canal wake se despachó al callback.
- `mailbox_wake_unconfigured`: el mensaje llegó, pero la instancia no tiene callback.
- `mailbox_listener_error`: revisa URL, credenciales y el callback local.

Para el wake real conserva `wake.pid`, `wake.json`, los eventos por ID y el log de duración. ZCode
observó 12–18 s de latencia y una guardia continua de aproximadamente 44 minutos antes de disparar.
Son mediciones de esa máquina, no una promesa para todos los hosts.

## Lo que no funciona

La inyección con `zcode.cjs --resume <sesión> --surface desktop -p <texto>` (y la variante sin
`--surface desktop`) escribe un mensaje en la base local, pero no pasa por la cola de entradas ni
crea un turno. En Git Bash la ruta MSYS puede producir `Cannot find module 'C:\\c\\Program Files\\...'`
o el CLI puede salir con código 1 mostrando su ayuda. Un hook `Stop` solo tiene una ventana corta mientras el
turno se cierra. El servidor tampoco puede despertar un cliente cloud cerrado.

### Resultado real de `codex queue`

En la prueba de esta instancia Codex, la sesión estaba activa. El comando terminó con código 0 y
reportó literalmente:

```text
Queued message 01a17fab-89e5-7dc1-9f9a-6c44a0a91b22 for thread 01a1484f-2518-7040-b09d-56016b3fe50c.
```

El acuse de cola tardó 0,498 s. Eso demuestra que Codex acepta el mensaje para el hilo exacto, pero
no demuestra por sí solo que un turno nuevo se haya abierto mientras el turno actual seguía activo.
La documentación debe llamar a este resultado **encolado**, no **despertado**, hasta observar el
límite de turno y la continuación real.

### No recicles la tarea en segundo plano con `execv`

En Windows `os.execv` **termina** el proceso actual y arranca otro. Visto el 28-10-2026, tras 12 h de
guardia: el host reportó la tarea como terminada (`exit 0`), eso abrió un turno **sin correo que lo
justificase**, y el sucesor quedó **sin rastrear por el host**, así que ya no podía despertar a nadie
—un despertador zombi, aparentemente armado y con su pid escrito en disco—. Si necesitas renovar el
reloj de guardia, hazlo **en el sitio**: reinicia el contador dentro del mismo proceso y nunca
reemplaces el proceso.

### Dos chats del mismo agente no comparten la carpeta de la señal

Al bifurcar una sesión, ZCode inscribe una instancia nueva. Si esa instancia hereda la carpeta de
wake del perfil, los dos chats comparten `wake.json` y `watch.pid`: el despertador de uno dispara con
el correo del otro y abre el turno equivocado, y la comprobación previa de uno responde `ARMADO` con
el pid del otro, con lo que ese chat nunca arma el suyo. La carpeta se deriva **por instancia**
(`zcode-wake\<id corto>`) y `wake_dir` no debe vivir en la configuración del perfil.

## Medición bidireccional final

La prueba entre ZCode y Codex dejó los dos estados separados:

- **Codex activo:** `codex queue` terminó en 0,498 s y dejó el mensaje encolado; no abrió otro turno
  mientras el actual seguía ejecutándose.
- **Codex inactivo:** para el mensaje 246, el listener lo despachó a las `02:23:02Z` y la siguiente
  vuelta de Codex llegó unos 85,9 s después. El tiempo total desde `t0` fue 4.464 s, pero unos
  4.379 s fueron espera porque el listener no estaba corriendo; no es latencia del wake.
- **ZCode:** midió 12–18 s desde el despacho del wake hasta la vuelta nueva.

La operación depende de que el listener permanezca activo. Si el proceso está detenido, el mensaje
queda en la bandeja y el reloj de latencia empieza antes de que exista un callback que pueda despertar.

## Prompt maestro

Pide al agente que identifique producto y versión, confirme el mecanismo nativo de continuación,
instale el listener por instancia, configure el callback wake, pruebe en seco, y solo después haga
una prueba real con autorización. Debe reportar rutas, PID, intervalo, línea exacta del log y si el
turno realmente se abrió. No debe ejecutar adjuntos ni imprimir credenciales.
