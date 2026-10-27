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

La inyección con `zcode.cjs --resume ... -p ...` escribe un mensaje en la base local, pero no pasa
por la cola de entradas ni crea un turno. Un hook `Stop` solo tiene una ventana corta mientras el
turno se cierra. El servidor tampoco puede despertar un cliente cloud cerrado.

## Prompt maestro

Pide al agente que identifique producto y versión, confirme el mecanismo nativo de continuación,
instale el listener por instancia, configure el callback wake, pruebe en seco, y solo después haga
una prueba real con autorización. Debe reportar rutas, PID, intervalo, línea exacta del log y si el
turno realmente se abrió. No debe ejecutar adjuntos ni imprimir credenciales.
