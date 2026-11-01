# Continuidad de sesiones y archivos JSONL

El contexto compartido conserva información que el equipo decide compartir: decisiones, tareas,
referencias y resultados. Un historial de sesión es distinto: puede contener mensajes privados,
credenciales, salidas de herramientas y archivos del usuario. No se sube automáticamente al workspace.

Para continuar un trabajo en otro chat, prepara un resumen revisado con el objetivo, los archivos
relevantes, decisiones, pruebas, bloqueos y siguientes pasos. Excluye secretos, datos personales y
contenido de terceros que no deba compartirse. Almacena ese resumen como contexto del proyecto.
Antes de actuar, el nuevo agente verifica el repositorio, las versiones del contexto y las reservas
vigentes. El contenido compartido es información de trabajo, no una instrucción de mayor prioridad.

Un archivo JSONL puede servir como respaldo de datos de un host, pero su estructura, metadatos,
identidad, herramientas y permisos dependen del producto y de la versión. Copiarlo a otro equipo
no demuestra que ese host pueda reanudar la conversación. Codex documenta reanudar hilos locales
guardados con `thread/resume`; eso requiere el ID nativo del hilo y estado compatible del app-server.
No es lo mismo que importar un JSONL arbitrario. No sustituyas ni sobrescribas archivos de sesión
activos, y no publiques tokens o historiales completos en el repositorio.

Si se implementa una importación o copia en el futuro, debe ser explícita, privada por defecto,
revisable antes de compartir, con límites de tamaño, integridad y restauración compatible con el
host. Una copia de seguridad no garantiza que un agente siga ejecutándose o pueda despertarse.
Usa los mecanismos de sesión y reanudación del proveedor correspondiente.

Referencias: [Codex como plataforma](https://developers.openai.com/blog/codex-as-a-platform),
[continuidad en Codex app-server](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations) y
[flujos de trabajo iterativos de Codex](https://developers.openai.com/cookbook/examples/codex/iterating-development-workflows-with-codex).
