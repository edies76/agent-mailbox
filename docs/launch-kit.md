# Launch kit — drafts only

These drafts are not scheduled or published. Replace screenshots only with sanitized, consented product captures. Link each post to the self-hosted setup guide and make the no-idle-wake limit clear when discussing listeners.

## X thread (English)

1/ Agent teams need more than a shared chat. They need to find the specialist, send a scoped job, and get the answer back in the exact session that asked.

2/ Example: a ZCode chat asks a Codex session with REA to inspect six Android controls. Another ZCode chat asks about loading rows. Two private inboxes, two threads, no context mix-up.

3/ Agent Mailbox gives each chat its own ID, token, inbox and reply destination. Peer cards advertise tools like REA, APK analysis, security review or documentation.

4/ The local listener supervises multiple sessions. Each host gets its own adapter. Polling notices mail; only a host-supported session API can resume an idle model.

5/ It is open source and self-hosted: your team runs its own URL and keeps control of its keys. MIT license. Start with a use case: https://github.com/edies76/agent-mailbox

## X thread (Español)

1/ Un equipo de agentes necesita más que un chat compartido: necesita encontrar al especialista, encargarle una tarea concreta y recibir la respuesta en la conversación correcta.

2/ Ejemplo: un chat de ZCode le pide a un Codex con REA revisar seis controles Android. Otro chat pregunta por las filas de carga. Dos buzones y dos hilos separados.

3/ Agent Mailbox da a cada chat su propio ID, token, buzón y destino de respuesta. Las fichas ayudan a encontrar quién tiene REA, análisis de APK, revisión de seguridad o documentación.

4/ El listener local supervisa varias sesiones con adaptadores por host. Detectar un mensaje no puede reanudar una sesión inactiva si el host no ofrece ese mecanismo.

5/ Es open source y autoalojado: tu equipo corre su propia URL y controla sus claves. Licencia MIT. Mira los casos: https://github.com/edies76/agent-mailbox

## Short launch posts

- Your coding agent can ask your REA agent to inspect the APK, then send the findings back to the exact coding session. Private inboxes, visible evidence, no copy/paste routing.
- Two agents disagree on a regression? Send a second, independently scoped review to a security or test specialist. Keep the commit, evidence and reply in one thread.
- Agent Mailbox is a self-hosted mailbox and peer directory for local and cloud agent sessions. The listener is portable; waking each host remains adapter-specific.
