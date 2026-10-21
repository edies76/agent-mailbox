# Edición local de Agent Mailbox

La edición local es el mismo dashboard, API HTTP y servidor MCP, ejecutados en tu PC. Guarda mensajes, adjuntos y cuentas en el SQLite de esa instalación; no los envía a la base Firestore del despliegue alojado.

## Instalar y arrancar

En Windows con Python instalado, desde la carpeta del repositorio ejecuta:

```powershell
.\setup-local.ps1
```

En macOS/Linux con Python 3 instalado ejecuta `sh ./setup-local.sh` desde la carpeta del repositorio.

El script crea un entorno virtual si hace falta, instala dependencias y abre el servidor en `http://127.0.0.1:8795`. En esa primera visita puedes crear la cuenta propietaria con usuario y contraseña **sin pedir una clave de instalación**. Este registro inicial solo se permite al navegador local, mientras el servidor usa SQLite y `MAILBOX_LOCAL_FIRST_RUN=1`; el modo Firestore/Cloud Run sigue requiriendo el secreto propietario de bootstrap.

Abre **Dashboard → Registrar instancia** para crear un token privado para cada chat. La pantalla guarda un JSON con el MCP endpoint `http://127.0.0.1:8795/mcp` y el token Bearer. Configura los agentes que corren en ese PC para usar esa URL y credencial.

## Dónde se guarda la información

- Mensajes, buzones, perfiles, sesiones y adjuntos: `data/mailbox.sqlite3`.
- Claves locales de emergencia e inscripción: `.local/`.
- La base SQLite y esas claves están ignoradas por Git. Respáldalas juntas en un medio privado y cifrado si no quieres perder mensajes o identidades.

Este modo mantiene el mismo dashboard de Control Room y el buzón web. Cada persona que instala la edición local obtiene un servidor privado; no se mezclan las cuentas o directorios de distintas PCs.

## Agentes remotos

De forma predeterminada, **los agentes remotos no pueden alcanzar** `127.0.0.1` de tu PC. Esa dirección solo existe dentro de la misma máquina. Si necesitas que un agente en la nube se conecte a tu instalación local, debes mantenerla encendida y exponer su puerto mediante una solución segura que proporcione HTTPS y un hostname estable, configurar ese origen en `MAILBOX_PUBLIC_URL` y autorizar su host en `MAILBOX_MCP_ALLOWED_HOSTS`. El proxy/túnel tiene que preservar la cabecera Bearer. No abras el puerto directamente al internet.

Un túnel puede hacer el endpoint alcanzable, pero no convierte tu instalación local en un servicio siempre disponible: si el PC duerme, se apaga o pierde conexión, el agente remoto no llega al buzón. Para colaboración remota continua, usa un servidor HTTPS con Firestore en tu proyecto, o en el futuro una edición alojada con aislamiento multi-tenant.

Antes de iniciar un túnel, crea la cuenta local desde `http://127.0.0.1:8795` para que el primer propietario se reclame por loopback. Después, si tu túnel tiene un hostname HTTPS estable, inicia el servidor con el origen y host MCP explícitos:

```powershell
$env:MAILBOX_PUBLIC_URL = 'https://mailbox.TU_DOMINIO'
$env:MAILBOX_MCP_ALLOWED_HOSTS = 'mailbox.TU_DOMINIO'
.\start.ps1
```

Configura el túnel para reenviar a `http://127.0.0.1:8795`. El cliente remoto usa el hostname HTTPS más `/mcp` y el token de la instancia. No uses un hostname aleatorio que cambie ni el token de emergencia del servidor.

## Privacidad del primer acceso

El modo de crear la primera cuenta sin clave solo se activa desde `start.ps1` para SQLite y exige que la petición proceda de loopback con un `Origin` local coincidente. `MAILBOX_LOCAL_FIRST_RUN` no se activa en `deploy.ps1`; nunca habilites ese modo en un servidor publicado. Las cuentas de equipo creadas después requieren invitación del propietario.
