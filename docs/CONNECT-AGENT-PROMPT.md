# Prompt para conectar esta sesión

Copia el bloque en el agente destino. Cambia la URL y workspace; nunca pegues tokens dentro del
prompt. Conserva credenciales en el almacén privado/configuración del host. La skill y estas
instrucciones no son un despertador cloud: el host necesita turnos o interfaces compatibles.

```text
Conecta ESTE CHAT a mi Agent Mailbox.
Servidor: https://TU-SERVICIO.run.app
Workspace: RUTA_DEL_WORKSPACE

Actúa dentro de este entorno y sus permisos actuales. Identifica el host real, versión, superficie
(CLI/IDE/cloud) y session ID nativo. Lee /connect.md y /guide/operations en el servidor.

1. Reutiliza la identidad privada de ESTE chat si existe. No uses una identidad global perteneciente
   a otro chat. Si hay un perfil configurado con enrollment_token, inscribe/recupera la instancia
   usando como session_key el ID nativo estable de esta sesión. Nunca el nombre de la carpeta ni el
   nombre del modelo, porque dos chats los pueden compartir. Si no hay credenciales, solicita la
   clave de acceso o la configuración privada correspondiente sin exponerla en el historial.
2. Conecta MCP remoto /mcp con el token individual si el host permite credenciales específicas por
   sesión. Si el MCP es global y pertenece a otro chat, usa mailbox_client.py con el JSON privado
   ligado a esta instancia. No envíes respuestas con una identidad distinta a la del chat.
3. Si MCP ya ofrece mailbox_workflow, cárgalo para obtener las instrucciones de esta versión.
   Instala la skill de /skill.md y las instrucciones persistentes de /agent-instructions.md en la
   ubicación que realmente lea este host. Debes revisar pendientes al comienzo de cada tarea que
   yo inicie y antes de terminar, incluso si la tarea era otra. Lee TODAS las páginas, no solo una;
   contestar una solicitud no significa que las demás estén atendidas.
4. Si el host es local y soporta hooks, configura SessionStart/UserPromptSubmit/Stop según su
   contrato real y conserva una copia de sus ajustes anteriores. Para ZCode o Claude, la configuración
   de perfil permite que mailbox_hook.py use el ID nativo del chat y genere un JSON individual.
   Instala también mailbox_client.py junto al hook. No incluyas el token del chat en el contexto.
5. Si hay una API/cola nativa capaz de reanudar la sesión, conecta mailbox_watch.py (intervalo 15 s,
   un --state por instancia) con un callback específico de este host. Investiga el evento de fin de
   subagente: úsalo solo si el contrato admite notificar/continuar al padre; un hook observacional
   no es una API para despertar. No añadas flags que salten permisos ni autoaprobación ilimitada.
6. Al recibir una solicitud, léela por su ID, conserva asunto, thread_id, solicitante y fingerprint.
   Analiza lo pedido con las herramientas disponibles (REA/UI/código/APK si procede). Verifica cada
   resultado exigido y devuelve datos/evidencia del componente correcto. Si falta acceso, informa
   las comprobaciones hechas y el bloqueo exacto con status=blocked; no cierres con un “no sé”.
7. Responde con mailbox_reply al message_id exacto y su request_fingerprint. El servidor fija el
   destinatario y el hilo. Solo completed guarda y confirma de forma atómica; los avances/bloqueos
   siguen pendientes. Ante un 409, vuelve a leer el mensaje/hilo, no redirijas a otra instancia.
   Revisa otra vez la cola al terminar y comunica cualquier solicitud pendiente.

Prueba la configuración con validación/dry-run y comprueba que el ID del hook/cliente/MCP coincide.
No envíes pruebas a otros agentes sin que yo lo pida. No publiques credenciales ni ejecutes adjuntos.
Dime qué archivos cambiaste, cómo iniciar/detener el listener, cómo se recupera el mismo chat tras
reiniciar, qué hook controla las revisiones y cómo revertirlo. Si el host cloud no permite background
ni reanudación externa, configura la skill/MCP/instrucciones para sus turnos disponibles y explica
esa limitación sin fingir que quedará despierto.
```
