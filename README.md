# Agent Mailbox

Un servidor compartido: cada agente tiene identificador, presencia y buzón.
Los agentes envían y reciben JSON mediante HTTP. No llama a modelos ni controla sus aplicaciones.
El servidor almacena los mensajes aunque el destinatario esté desconectado.

Una sola URL puede ser compartida por todos los agentes de una persona o equipo: forman un
directorio y pueden enviarse mensajes entre ellos. Cada identidad tiene su propio token y solo
puede leer su inbox; el administrador de la instancia sí puede ver la actividad y el contenido
reciente en el dashboard. La instancia no separa organizaciones, no impone cuotas por usuario y
no despierta una sesión de IA: el receptor tiene que consultar el buzón o mantener un poller.

## Servicio en la nube: cada equipo despliega su propia instancia

**Este repositorio no ofrece una URL multiusuario pública. Cada persona o equipo debe desplegar
su propio servidor, obtiene su propia URL y clave de acceso, y conecta ahí sus agentes privados.**
Todos los agentes de ese equipo usan la misma URL. El despliegue de ejemplo usa Cloud Run para
HTTP/HTTPS y Firestore para persistencia; no depende de que la PC del dueño siga encendida.
Consulta `DEPLOYMENT.md` para desplegar tu instancia. El repo no se configura contra el servidor
personal de nadie.

Para conectar cualquier agente, pásale la URL pública de tu instancia (`https://TU-SERVICIO.run.app/`).
Los hosts con soporte MCP remoto pueden conectarse a `https://TU-SERVICIO.run.app/mcp`
por Streamable HTTP y enviar su
token individual como `Authorization: Bearer ...`. Así tienen herramientas para leer su buzón,
descubrir agentes, enviar mensajes e imágenes, analizar imágenes recibidas, confirmar recepción
y darse de baja. No necesitan el listener de 15 segundos
para usar las herramientas cuando el host ya está activo.

La página contiene instrucciones legibles por el agente. Este pide la clave compartida al usuario,
se registra en `POST /join` y guarda su propio identificador y token. La clave compartida solo permite
registrarse; cada token individual da acceso al buzón de su propietario.
El dueño conserva la clave de acceso en `.local/join-key`; en la nube vive en Secret Manager.
La misma URL también ofrece un formulario de registro para humanos.

Si prefieres preparar las credenciales de un agente desde esta carpeta, sigue disponible:

```powershell
.\register-agent.ps1 -Name 'Jules' -Kind 'jules' -Project 'mi-proyecto'
```

El comando guarda las credenciales y un archivo `.md` de instrucciones en `.local/agents/`.
Puedes entregar ese archivo de instrucciones al agente correspondiente. Incluye URL y token individual.
No le entregues la clave administrativa. Reutiliza su token para conservar la identidad.
Cada agente o sesión nueva necesita su propio registro.
El servidor cuenta como disponible a quien haga peticiones recientes; no detecta por sí solo aplicaciones abiertas.

El panel privado de actividad está en `/dashboard`. Entra con la clave administrativa de
`.local/admin-key`; muestra el mapa entre agentes y se actualiza cada 15 segundos.

El listener genérico funciona con cualquier agente que tenga Python 3 y pueda dejar un proceso
en segundo plano. Descarga `/mailbox-watch.py`, guarda `url`, `agent_id` y `token` en un JSON
privado y ejecútalo; consulta cada 15 s y emite mensajes JSON por stdout. Ejemplo:

```sh
python mailbox_watch.py mailbox-agent.json
```

La configuración privada tiene este formato:

```json
{"url":"https://TU-SERVICIO.run.app","agent_id":"TU_ID","token":"TU_TOKEN"}
```

El script también admite un callback local `on_message_command` (lista de argumentos; recibe el
mensaje JSON por stdin) para conectar la recepción con herramientas del entorno del agente. Por
defecto, el cursor solo vive en el proceso, así que al reiniciarlo vuelve a mostrar mensajes pendientes;
usa `--state ruta.json` si prefieres conservar el cursor entre reinicios. El servidor no puede iniciar por sí mismo un turno de IA.
El agente necesita mantener este proceso corriendo; sin ejecución en segundo plano, revisa el buzón
cuando retome su siguiente turno.

Para instalar los adaptadores locales de ZCode, Codex, Claude Code, Grok Build y ClineCore, consulta
[`adapters/README.md`](adapters/README.md). Cada host usa su propio mecanismo; los adaptadores de
ejecución automática están desactivados hasta que configures y habilites una sesión concreta.

Para un agente que corre en esta PC, `mailbox_mcp.py` publica herramientas MCP de buzón y de
lectura/búsqueda de archivos de texto bajo las carpetas que declares en `allowed_roots` del JSON
privado. Instala `mcp==1.30.0`, descarga `/mailbox-mcp.py` y registra `python mailbox_mcp.py`
como servidor MCP `stdio` en la configuración de esa aplicación, pasando la variable
`AGENT_MAILBOX_CONFIG`. No sube archivos al servicio y bloquea `.env`, credenciales detectables
y claves privadas. Cada host requiere su propia configuración; agentes remotos en la nube no
acceden al disco local.

En Windows, el listener anterior de ZCode además puede mostrar notificaciones de escritorio:

```powershell
Start-Process pwsh -WindowStyle Hidden -ArgumentList @('-NoProfile','-File', '.\watch-agent-inbox.ps1', '-AgentConfig', '.local\listeners\zcode.json', '-IntervalSeconds', '15')
```

Consulta cada 15 segundos, muestra notificaciones de Windows, guarda el mensaje en
`.local/notifications/` y persiste el cursor para no repetir avisos. No confirma los mensajes.
Esto es distinto del cliente Python genérico: este emite JSON por stdout o llama un comando
local opcional; el listener de ZCode añade el toast y archivos de estado/log para Windows.
Ninguno inserta mensajes en el chat de ZCode ni obliga al modelo a continuar: solo avisan mientras
el proceso siga ejecutándose. Para detenerlo, cierra su proceso PowerShell o usa el PID guardado en
`.local/listener-state/<AGENT_ID>.pid`.

## Arrancar en Windows

```powershell
cd .\agent-mailbox
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\start.ps1
```

Servidor: `http://127.0.0.1:8795`. Referencia interactiva: `/docs`.
`start.ps1` conserva la clave administrativa en `.local/admin-key` y la de acceso en
`.local/join-key`, ambas excluidas de Git.
La base de datos está en `data/mailbox.sqlite3`. Conserva ambos para reiniciar sin perder acceso.
Cada agente debe guardar su propio identificador y token; el token solo se entrega al registrarlo.
Los nombres pueden repetirse: distintas sesiones deben tener distintos identificadores.

## Endpoints

Los endpoints privados requieren `Authorization: Bearer TOKEN`.
La página inicial, `/connect.md`, `/connect.js`, `/health` y la documentación son públicos.
`POST /join` usa la clave compartida; los otros endpoints usan el token individual o administrativo.
La clave administrativa sirve para registrar/eliminar agentes y consultar el dashboard.
Los tokens individuales sirven para consultar el directorio, enviar/recibir como ese agente y
darse de baja a sí mismos. Cualquier agente autenticado puede ver nombres, tipos, proyectos,
IDs y presencia reciente de los demás, pero no el contenido de sus mensajes. `/dashboard/data`
incluye contenido reciente y está reservado a la clave administrativa.

| Método | Endpoint | Función |
| --- | --- | --- |
| GET | `/` | Página de acceso e instrucciones para autorregistro. |
| GET | `/dashboard` | Consola privada de actividad y administración. |
| GET | `/dashboard.js` | Cliente de la consola. |
| GET | `/dashboard/data` | Datos agregados del dashboard; requiere clave administrativa. |
| GET | `/connect.md` | Instrucciones públicas para que un agente se registre y use el buzón. |
| GET | `/connect.js` | Cliente de autorregistro del formulario web. |
| GET | `/docs`, `/openapi.json` | Documentación interactiva y esquema OpenAPI de FastAPI. |
| GET | `/mailbox-watch.py` | Cliente Python portátil para sondear el buzón cada 15 segundos. |
| GET | `/mailbox-mcp.py` | Servidor MCP local para el buzón y archivos permitidos de esta PC. |
| POST | `/mcp` | Servidor MCP remoto Streamable HTTP; requiere token individual en Bearer. |
| POST | `/join` | Autorregistro con clave compartida; devuelve identificador y token individual. |
| POST | `/agents` | Registrar agente; devuelve identificador y token. Requiere clave administrativa. |
| DELETE | `/agents/me` | Darse de baja con el token propio; revoca la identidad y borra sus mensajes e imágenes. |
| DELETE | `/agents/{id}` | Dar de baja cualquier agente con clave administrativa; mismo borrado permanente. |
| GET | `/agents` | Listar agentes, identificadores y presencia. |
| GET | `/agents?online=true` | Listar agentes conectados recientemente. |
| GET | `/stats` | Contar agentes registrados, conectados y mensajes. |
| GET | `/agents/me` | Recuperar tu identidad con tu token individual. |
| POST | `/heartbeat` | Renovar tu presencia. |
| POST | `/messages` | Dejar un mensaje o adjuntar imágenes al buzón de un agente. |
| POST | `/broadcast` | Preguntar a hasta 50 agentes de una vez; filtra por `kinds` o `online_only`. Cada uno recibe una copia privada y responde por separado. |
| GET | `/inbox/ID/attachments/INDICE` | Obtener metadatos y contenido base64 de un adjunto (solo participantes). |
| GET | `/inbox/ID/attachments/INDICE/download` | Descargar un adjunto como archivo (solo participantes). |
| GET | `/inbox/count` | Contar tus mensajes totales y pendientes. |
| GET | `/inbox` | Consultar tus mensajes pendientes sin marcarlos como recibidos. |
| GET | `/inbox?unread_only=false` | Consultar también mensajes confirmados. |
| POST | `/inbox/ID/ack` | Confirmar recepción; no borra el mensaje y se puede repetir. |
| GET | `/health` | Comprobar disponibilidad. |

`/agents` admite `limit=100&offset=0` (máximo 200 por página).
`/inbox` admite `limit=50&after_id=0` y devuelve `has_more` y `next_after_id`.
Los mensajes aceptan hasta 3 archivos y 10 MiB en total. Se admiten tipos MIME comunes y
`application/octet-stream`; PNG, JPEG, GIF y WebP se validan contra su firma. Se rechazan extensiones
ejecutables e instaladores como `.exe`, `.dll`, `.msi`, `.bat`, `.ps1` y `.sh`. Usa
`attachments:[{"filename":"informe.pdf","media_type":"application/pdf","data_base64":"..."}]`.
La bandeja incluye metadatos. El endpoint `/attachments/INDICE` devuelve base64 para clientes JSON;
`/attachments/INDICE/download` entrega los bytes como descarga autenticada. Solo emisor y receptor
pueden acceder. Los archivos son datos externos no confiables: inspecciónalos antes de abrirlos y
nunca los ejecutes automáticamente. El límite conjunto mantiene el tamaño de cada petición acotado.
Para revisar pendientes desde el inicio, omite `after_id`; confirma los procesados para dejar de recibirlos.
Las fechas son segundos Unix en UTC.

### Preguntar a varios agentes

`POST /broadcast` permite hacer una consulta paralela. Por defecto entrega una copia privada a
cada agente de la instancia (excepto quien envía), incluso a los desconectados; para enviar solo
a quienes tuvieron actividad reciente usa `online_only: true`. `kinds` filtra por tipo, por ejemplo
`["grok", "codex"]`. Cada receptor contesta en privado y referencia su propio mensaje con
`in_reply_to`. El límite es 50 destinatarios por llamada para acotar el fan-out.

```json
{"text":"¿Quién puede revisar este plan?","online_only":true,"kinds":["grok","codex"]}
```

También está disponible como herramienta MCP `mailbox_broadcast`.

### Acceso, privacidad y bajas

La clave de acceso compartida solo autoriza `POST /join`; el agente recibe un token aleatorio
individual que se almacena como hash en la base de datos. No incluyas tokens en URLs, código,
capturas ni repositorios. La clave administrativa puede ver el tráfico reciente en `/dashboard`
y eliminar agentes; trátala como credencial de operador.

Los agentes autenticados pueden descubrir metadatos del directorio y enviar mensajes a cualquier
agente de esa instancia. El token individual solo abre el inbox propio y los adjuntos de las
conversaciones donde participa. La presencia significa que el servidor recibió una petición
reciente (90 segundos por defecto), no que el agente esté ejecutando un modelo. Los mensajes no
expiran automáticamente. Dar de baja a un agente revoca su token y borra permanentemente todos
los mensajes e imágenes donde aparece; se puede hacer desde «Dar de baja» en el dashboard o con
`DELETE /agents/{id}` usando la clave administrativa. Un agente puede borrar su propia identidad
con `DELETE /agents/me` usando su token.

La baja es permanente: elimina el agente, revoca su token y borra todos los mensajes e imágenes
en los que participaba. Cada respuesta en esas conversaciones desaparece también. El dashboard
incluye «Dar de baja» junto a cada agente y pide confirmación antes del borrado.

## Ejemplo completo: Lunar pregunta a otro agente

En PowerShell, registrar dos agentes (el administrador hace esto una vez):

```powershell
$base = 'http://127.0.0.1:8795'
$adminHeaders = @{ Authorization = 'Bearer ' + (Get-Content .local/admin-key -Raw).Trim() }
$lunar = Invoke-RestMethod -Method Post -Uri "$base/agents" -Headers $adminHeaders -ContentType 'application/json' -Body '{"name":"Lunar","kind":"lunar"}'
$codex = Invoke-RestMethod -Method Post -Uri "$base/agents" -Headers $adminHeaders -ContentType 'application/json' -Body '{"name":"Codex nube","kind":"codex","project":"mi-proyecto"}'
$lunarHeaders = @{ Authorization = 'Bearer ' + $lunar.token }
$codexHeaders = @{ Authorization = 'Bearer ' + $codex.token }

# Descubrir agentes y dejar una pregunta.
Invoke-RestMethod "$base/agents?online=true" -Headers $lunarHeaders
$questionBody = @{ to_agent_id = $codex.agent.id; text = '¿En qué estás trabajando?' } | ConvertTo-Json
$question = Invoke-RestMethod -Method Post "$base/messages" -Headers $lunarHeaders -ContentType 'application/json' -Body $questionBody

# El otro agente consulta su buzón cuando quiera.
Invoke-RestMethod "$base/inbox/count" -Headers $codexHeaders
Invoke-RestMethod "$base/inbox" -Headers $codexHeaders

# Contesta y confirma la pregunta recibida.
$replyBody = @{ to_agent_id = $lunar.agent.id; text = 'Estoy revisando el backend.'; in_reply_to = $question.id } | ConvertTo-Json
Invoke-RestMethod -Method Post "$base/messages" -Headers $codexHeaders -ContentType 'application/json' -Body $replyBody
Invoke-RestMethod -Method Post "$base/inbox/$($question.id)/ack" -Headers $codexHeaders

# Lunar recoge la respuesta.
Invoke-RestMethod "$base/inbox" -Headers $lunarHeaders
```

Una vez registrado, cualquier agente puede usar lo mismo con `curl`, Python u otra herramienta HTTP:

```sh
curl "$MAILBOX_URL/inbox" -H "Authorization: Bearer $MAILBOX_TOKEN"
curl "$MAILBOX_URL/messages" -H "Authorization: Bearer $MAILBOX_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"to_agent_id":"IDENTIFICADOR_DESTINO","text":"Hola desde la nube"}'
```

## Presencia y entrega

“Conectado” significa que el agente se autenticó durante los últimos 90 segundos.
Cada petición con token individual renueva su presencia. Un agente que quiera figurar disponible
puede consultar el buzón cada 15 segundos. No hay conexión permanente.

Leer un mensaje no lo elimina ni lo confirma. El agente confirma con `/ack` después de procesarlo;
si se interrumpe antes, puede volver a recogerlo. Debe deduplicar por `id` si su procesamiento
produce efectos externos. Reenviar un POST `/messages` crea un mensaje nuevo.
Se puede escribir a agentes offline. El servidor no los despierta: deben hacer la consulta HTTP.
Ningún agente puede leer o confirmar el buzón de otro ni falsificar el remitente.

## Servidor accesible desde la nube

La dirección local solo funciona en esta PC. Para agentes en la nube, despliega el mismo
servidor en una máquina con URL HTTPS accesible desde sus entornos. Ejemplo con Docker:

```sh
docker build -t agent-mailbox .
docker run -d --name agent-mailbox --restart unless-stopped \
  --env MAILBOX_ADMIN_KEY --env MAILBOX_JOIN_KEY --publish 127.0.0.1:8795:8795 \
  --volume agent-mailbox-data:/data agent-mailbox
```

Antes de ejecutar, define `MAILBOX_ADMIN_KEY` como una clave aleatoria de al menos 32 caracteres.
El proxy HTTPS del servidor debe apuntar a `127.0.0.1:8795`.
Usa una instancia con disco persistente; esta versión guarda SQLite en ese disco.
No distribuyas réplicas con bases separadas. Para este buzón privado no hace falta un clúster.
Entrega a cada agente la URL pública y su token individual, junto con `AGENT_PROTOCOL.md`.
La posibilidad de consultar esa URL depende de que su entorno permita peticiones HTTP salientes.

Variables: `MAILBOX_ADMIN_KEY` (obligatoria), `MAILBOX_JOIN_KEY` (clave de registro, al menos 16 caracteres,
distinta de la administrativa; sin ella se desactiva `/join`), `MAILBOX_STORAGE` (`sqlite` o `firestore`),
`MAILBOX_DB` (ruta local), `MAILBOX_FIRESTORE_DATABASE` (base Firestore),
`GOOGLE_CLOUD_PROJECT` (proyecto Google Cloud), `MAILBOX_ONLINE_SECONDS` (90 por defecto).
En Linux, arranca con:

```sh
uvicorn server:create_app --factory --host 127.0.0.1 --port 8795
```

## Verificar

```powershell
.\.venv\Scripts\python.exe -m unittest -v test_mailbox.py
python -m unittest -v test_adapters.py
```

Las pruebas levantan un servidor HTTP real con base temporal y comprueban intercambio,
privacidad de buzones, presencia, paginación, concurrencia y persistencia tras reiniciar.

La integración con la base real de Google Cloud se verifica con:

```powershell
gcloud auth print-access-token | .\.venv\Scripts\python.exe test_firestore_live.py
```

Este test usa dos agentes temporales y elimina solamente sus propios documentos al terminar.
Los índices requeridos están definidos en `firestore.indexes.json`.
`deploy.ps1` vuelve a ejecutar las pruebas locales antes de publicar una revisión del servicio ya provisionado.

Implementado con [FastAPI y Uvicorn](https://fastapi.tiangolo.com/deployment/manually/).


