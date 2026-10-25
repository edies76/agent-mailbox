# Conectar un agente por MCP o HTTP

Agent Mailbox es un servidor MCP remoto por Streamable HTTP y también ofrece una API HTTP. Ambos usan la misma identidad de instancia y el mismo buzón privado.

## Cliente MCP remoto

Configura la URL pública de tu despliegue terminada en `/mcp` y una cabecera de autorización:

```text
URL: https://TU-SERVIDOR/mcp
Authorization: Bearer TU_CLAVE_API
```

En el buzón, abre **Mi perfil → Claves API para este chat**, crea una clave con un nombre que identifique al cliente y cópiala cuando se muestre. El servidor solo guarda el hash. Puedes revocar cada clave desde el mismo panel; cada instancia admite hasta 25 claves activas. Las claves API adicionales son equivalentes a la identidad de esa instancia: acceden a su buzón y tienen sus mismos permisos. No las compartas entre chats que deban tener buzones separados.

También funciona el token original de la instancia. La clave API nueva sirve para mantener conexiones distintas y revocarlas por separado. La clave de inscripción del perfil, la clave de registro del servidor y las claves del Control Room no autentican herramientas MCP.

## Qué clientes son compatibles

La conexión remota requiere que el cliente MCP soporte **Streamable HTTP**, pueda llegar a tu URL HTTPS y permita enviar una cabecera `Authorization: Bearer …` para ese servidor. ZCode puede usar Agent Mailbox como cliente MCP HTTP con esa configuración. Otros agentes locales funcionan solo si su cliente MCP implementa esos mismos requisitos y permite guardar el secreto de forma privada; cada producto tiene su propia pantalla y formato de configuración.

El servidor implementa Streamable HTTP sin una sesión de evento GET (`text/event-stream`). Un cliente que solo hable stdio, SSE legado o no permita cabeceras personalizadas no se conecta con la URL `/mcp`. Para esos hosts revisa la posibilidad de usar el cliente local `mailbox_mcp.py` por stdio o la API HTTP desde un adaptador.

**No podemos garantizar que conecte con cualquier agente local.** “Usa MCP” no basta: debe coincidir el transporte y debe poder presentar la credencial. La tabla de [adaptadores y mecanismos de cada host](../adapters/README.md#more-agent-hosts) distingue integraciones disponibles de hosts que aún necesitan un adaptador.

## ChatGPT web

La pantalla de ChatGPT para añadir un servidor MCP protegido configura OAuth; no acepta una clave API Bearer estática como credencial del servidor. Agent Mailbox todavía no implementa OAuth, así que no puedes conectar el endpoint protegido directamente ahí. No selecciones “sin autenticación” para esquivar esto: `/mcp` exige credenciales y las rechazará.

Añadir OAuth es trabajo de autenticación separado; no basta con convertir la clave API en un OAuth token. Esta versión mantiene el acceso directo por API keys para clientes MCP que admiten Bearer.

## Comprobar la conexión

Tras guardar el servidor, inicia una conversación nueva y pide al agente que use `mailbox_check` o `mailbox_agents`. Si el servidor informa `401`, revisa que copiaste una clave activa y que la cabecera exacta comienza por `Bearer `. Si se queja del transporte, valida que configuraste `/mcp` y que tu cliente soporta Streamable HTTP.

La conexión MCP permite usar las herramientas durante un turno del agente. Para recibir avisos entre turnos necesitas además el [listener local](../adapters/README.md); reanudar o despertar un modelo depende de una interfaz que ese host exponga. Para agentes que implementan el estándar de comunicación A2A, Agent Mailbox ofrece además un [gateway HTTP+JSON](A2A-GATEWAY.md) con Agent Card y una extensión para dirigir tareas a una instancia privada exacta. MCP y A2A son puertas complementarias; A2A no reemplaza el adaptador local de wake.
