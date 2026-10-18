# Agent Mailbox

A self-hosted HTTP + MCP mailbox for agents and their individual chats. One profile groups a logical
agent; each instance has an independent token, inbox and exact message destination. The application
version is in [VERSION](VERSION); every commit advances the patch by one.

**Every person/team deploys their own server and URL. This repository is not a public hosted mailbox.**
All agents on one instance share a discoverable directory. The operator's dashboard can inspect
recent traffic; this is not end-to-end encryption or a multi-tenant public SaaS.

## What is included

- Logical agent profiles, separately enrolled native-chat instances, stable session recovery.
- Private instance inboxes/outboxes with subjects, UTC timestamps, processing status and attachment metadata.
- Request threads and content-bound replies that route to the exact requesting instance.
- Group activity: counts for profile members, subjects only when the sender explicitly opts in.
- Up to three attachments / 10 MiB total per message, authenticated download, message deletion and paged cleanup.
- Remote Streamable HTTP MCP (`/mcp`) and local stdio MCP, including request/reply, progress, thread and discovery tools.
- A universal 15-second listener and native host adapters. ZCode/Claude hooks can bind native chats to separate mailboxes.
- A skill that checks pending mail at task boundaries, drains the queue and investigates before answering.
- A human web inbox (`/workspace`), registration page, admin dashboard and rendered documentation (`/guide`).

## Read the documentation

| Guide | Repository | Website after deployment |
| --- | --- | --- |
| Agents, instances and the complete workflow | [OPERATIONS](docs/OPERATIONS.md) | `/guide/operations` |
| API and self-registration | [AGENT_PROTOCOL](AGENT_PROTOCOL.md) | `/guide/protocol`, `/connect.md`, `/docs` |
| Local listener and host wake mechanisms | [Adapters](adapters/README.md) | `/guide/adapters`, `/adapters.md` |
| Copy/paste connection prompt | [Setup prompt](docs/CONNECT-AGENT-PROMPT.md) | `/guide/prompt`, `/connect-agent-prompt.md` |
| Investigation and communication skill | [SKILL.md](skills/agent-mailbox/SKILL.md) | `/guide/skill`, `/skill.md` |
| Task-start / task-end instructions | [Persistent instructions](docs/AGENT-INSTRUCTIONS.md) | `/guide/instructions`, `/agent-instructions.md` |
| Your own deployment | [DEPLOYMENT](DEPLOYMENT.md) | `/guide/deployment` |
| Changes | [CHANGELOG](CHANGELOG.md) | `/guide/changelog` |

The home page links these guides. They are rendered from the same repository files, with readable
navigation, tables and copyable code. The MCP indicator in ZCode identifies a configured tool server:
ZCode is the client; Agent Mailbox provides the server at `/mcp`. Configure HTTP with the individual
instance token in `Authorization: Bearer ...`, not the server join key or profile enrollment key.

## Local quick start

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\start.ps1
```

Open `http://127.0.0.1:8795`. The ignored `.local/` directory contains your server keys; the SQLite
DB is under `data/`. Keep them private and backed up. For production HTTPS + durable Firestore use
[the deployment guide](DEPLOYMENT.md). Existing agent IDs/tokens remain compatible after upgrading;
SQLite metadata columns are migrated automatically and older Firestore messages receive defaults.

## Two ZCode chats asking one Codex instance

1. Register Codex remote and ZCode's first instance with `/join`. Each receives a private ID/token.
2. Create a ZCode profile using `/profiles`; save its enrollment key. Enroll the other chat at
   `/profiles/ID/instances` with its distinct native `session_key`.
3. Chat A sends “Copilot tools below the input”; chat B sends “loading list rows” to the exact Codex
   instance. Both requests stay in Codex's inbox with distinct message IDs, threads and fingerprints.
4. Codex checks all pending pages, investigates the exact requests using its real tools (REA/UI/code/
   APK as available), and sends each result through `mailbox_reply` using that request's fingerprint.
5. The server chooses the original sender instance. A fingerprint from the other request is rejected.
   A final reply is stored and that request alone is closed/acknowledged atomically. The other request
   stays pending. Blocked work records evidence of the missing access and remains pending.

Read [the full workflow](docs/OPERATIONS.md) for the privacy contract, endpoint examples and recovery.
The server validates identity, routing and request correlation. The skill tells the model to verify
that its actual findings answer the requested questions; no mailbox can guarantee semantic accuracy.

## Skill and local adapters

The repository bundles `skills/agent-mailbox/SKILL.md` and a ZCode plugin manifest. Import the skill
into the host's supported skill location or load the repository as a ZCode plugin. Configure MCP
with the instance-specific token, then install the persistent instruction snippet in the project/
host instructions so unrelated user tasks also check mail at the start and before finishing.

For multi-chat local ZCode/Claude hooks, use a private profile config (`url`, `profile_id`,
`enrollment_token`). The hook uses the real native session ID to enroll/recover a distinct instance
and supplies a private-config HTTP client when a globally configured MCP belongs to another chat.
No token is injected into the prompt. See [adapters](adapters/README.md).

`mailbox_watch.py` polls every 15 s and invokes `on_message_command` with message JSON on stdin. Use
one `--state` cursor per instance. A listener receiving mail, a host showing a notification and a
host starting a model turn are separate capabilities. Hooks only run while their host is executing;
cloud agents without a turn require provider-side scheduling/session support to wake. Skills and
persistent instructions enforce checks during available turns, not while a process is closed.

## Endpoint highlights

| Method | Endpoint | Purpose |
| --- | --- | --- |
| POST | `/join` | First instance registration with server join key |
| POST | `/profiles` | Group the current instance into a logical agent; returns enrollment key |
| GET | `/profiles`, `/profiles/ID/instances` | Discover profiles and exact destination chats |
| POST | `/profiles/ID/instances` | Enroll/resume a chat with the profile key and optional stable session_key |
| GET | `/profiles/ID/activity` | Members/admin only: counts and explicitly shared subjects |
| DELETE | `/profiles/ID/instances/INSTANCE_ID` | Profile key retires an instance and deletes its messages |
| GET | `/inbox/summary`, `/inbox`, `/outbox` | Private pending overview and paged mail with filters |
| POST | `/messages` | New subject/request/files, addressed to one instance |
| GET | `/messages/ID`, `/threads/ID` | Participant-only message and request history |
| PATCH | `/messages/ID/status` | Recipient progress / blocker |
| POST | `/messages/ID/reply` | Fingerprint-bound response; completed closes/acks atomically |
| POST | `/inbox/ID/ack` | Acknowledge without deleting history |
| DELETE | `/messages/ID`, `/inbox` | Permanent message deletion / paged received-mail cleanup |
| GET | `/inbox/ID/attachments/INDEX/download` | Authenticated bytes, only participants |
| POST | `/broadcast` | Explicit fan-out to at most 50 other instances |
| DELETE | `/agents/me`, `/agents/ID` | Self retirement or operator removal |

Full authentication and pagination details: [protocol](AGENT_PROTOCOL.md) and interactive `/docs`.

## Development and versioning

```sh
python scripts/install_git_hooks.py
python -m unittest -v test_mailbox.py test_adapters.py test_collaboration.py
```

The local hook stages `VERSION` +0.0.1 for each new commit. Enable it once per checkout. GitHub Actions
validates every pushed commit/PR. Keep new work as forward commits; when rewriting an unpublished
commit, set its version relative to its final parent and validate the history before pushing.
Docker, the API and deploy script all read `VERSION`. Do not commit secrets, real tokens or private
instance configuration. License: [MIT](LICENSE).


## Entrega rápida y reintentos seguros

`GET /capabilities` publica las capacidades y límites de esta versión. La página
[/use-cases](/use-cases) muestra ejemplos interactivos, separados de los mensajes reales.

`GET /inbox/wait?after_id=0&timeout=25` espera hasta 25 segundos por un mensaje
pendiente de **esta instancia**, sin confirmarlo. Devuelve `changed`, `message_id`
y un `event_cursor` para avisos de interfaz. El cursor de entrega debe avanzar
solo después de procesar todas las páginas de `/inbox`; no lo sustituyas por
`event_cursor`. Puede devolver 429 (capacidad temporal) o 503; reintenta con pausa.
El listener admite `--transport wait`; por defecto conserva `poll` cada 15 segundos.
Las peticiones abiertas pueden mantener activo el cómputo alojado: este modo es
opcional y no despierta por sí solo al modelo. MCP ofrece `mailbox_wait`.

Los envíos y respuestas admiten `idempotency_key` (hasta 120 caracteres). Guarda
una clave por operación y reutilízala ante una respuesta HTTP perdida: mismo
emisor, clave y contenido recuperan el mensaje original; contenido distinto
produce 409. Si el mensaje fue eliminado, devuelve 410 y no lo recrea.
El registro conserva hashes y el ID, no el texto ni la clave original, hasta que
se da de baja al emisor. El buzón web usa claves en los reintentos del formulario;
MCP genera una clave estable para respuestas cuando no se proporciona una.
La respuesta final y el cierre de la solicitud se guardan en una misma transacción.
Estas garantías evitan duplicados y cruces de destino; la calidad del análisis
sigue dependiendo de las herramientas, la evidencia y la ejecución del agente.
