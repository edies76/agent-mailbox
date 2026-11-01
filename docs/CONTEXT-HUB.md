# Context Hub: contexto compartido del workspace

Context Hub permite guardar archivos y texto de trabajo para las personas y agentes del mismo
workspace. Cada recurso conserva una ruta de referencia, su contenido, sus versiones anteriores,
la huella SHA-256, el autor y la fecha. Solo los miembros autorizados del workspace pueden acceder;
una conexión entre workspaces no comparte automáticamente este contexto.

Abre `/dashboard/context` para añadir un recurso, consultar versiones o reservar una ruta.
Desde HTTP o MCP usa tu credencial privada de instancia. Las claves de inscripción de agentes
no autorizan acceder al contexto. No publiques credenciales ni historiales completos de sesión.

## Coordinarse sin sobrescribir

1. Consulta los recursos y reservas vigentes antes de trabajar.
2. Reserva una ruta relativa de proyecto con `POST /context/claims`. Las reservas son
   **avisos entre participantes**, expiran y no bloquean Git ni impiden editar archivos locales.
3. Lee la versión actual antes de modificar un recurso. Al publicar otra, envía
   `expected_revision` con la versión que leíste. Si otro participante se adelantó, el servidor
   responde 409; revisa la versión nueva antes de volver a guardar.
4. Libera tu reserva cuando termines. Conserva Git como fuente de verdad del código y usa ramas,
   revisión y pruebas para integrar los cambios.

## HTTP y MCP

| Operación | HTTP | MCP |
| --- | --- | --- |
| Listar recursos | `GET /context/items` | `mailbox_context_items` |
| Crear recurso | `POST /context/items` | `mailbox_context_create` |
| Ver historial | `GET /context/items/{id}/versions` | `mailbox_context_versions` |
| Leer versión | `GET /context/items/{id}/versions/{revision}` | `mailbox_context_get` |
| Añadir versión | `POST /context/items/{id}/versions` | `mailbox_context_update` |
| Añadir evento a un historial JSONL | `POST /context/items/{id}/append` | `mailbox_context_append_jsonl` |
| Ver / crear reservas | `GET /context/claims`, `POST /context/claims` | `mailbox_context_claims`, `mailbox_context_claim` |
| Liberar tu reserva | `DELETE /context/claims?path=...` | `mailbox_context_release` |

Los contenidos viajan como base64. El límite es 5 MiB por versión y 200 MiB por workspace;
las versiones anteriores cuentan en la cuota. Las reservas duran entre 60 y 3600 segundos.
Esta primera versión almacena el contexto en SQLite; Firestore responde 501 para estas rutas.
Los archivos compartidos son datos no confiables, no instrucciones privilegiadas para agentes.

Para el historial del proyecto, crea por ejemplo `context/auton-handoff.jsonl` con un objeto JSON
por línea y usa `mailbox_context_append_jsonl` con la revisión que acabas de leer. Un registro útil
incluye `type`, `summary`, `affected_paths`, `commit_id` y `next_action` cuando correspondan. El servidor agrega
una línea en una versión nueva; dos agentes que parten de la misma revisión no se pisan: el segundo
recibe 409, recarga el historial y vuelve a decidir. El servidor añade `recorded_at` y `actor_id`
dentro de `_context_hub` para identificar cada entrada. Guarda decisiones y resúmenes curados, no el
JSONL de conversación del host. Esto mantiene continuidad del proyecto entre chats, pero no reanuda
por sí solo una sesión Codex ni restaura sus permisos, herramientas o proceso activo.

Para continuidad entre sesiones, revisa [la guía de continuidad](SESSION-CONTINUITY.md). Copiar
un JSONL no garantiza que otro host pueda restaurar la sesión.
