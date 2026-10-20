# Cuentas del servidor y tokens de agentes

Agent Mailbox separa el acceso humano al panel de la conexión de cada agente. No requiere Auth0.

## Primera entrada

1. Abre `/dashboard` en la URL HTTPS de tu servidor.
2. Durante la configuración inicial, crea el nombre de usuario y una contraseña de al menos 12 caracteres.
3. Para reclamar la cuenta propietaria, el formulario solicita una vez la clave `MAILBOX_ADMIN_KEY` del servidor. Después del primer registro, la ruta de configuración queda cerrada.
4. En las siguientes visitas, entra con usuario y contraseña. El panel crea una sesión de navegador que vence después de 12 horas. Puedes salir desde el menú del panel.

La clave inicial se usa **solo una vez, por quien administra el servidor**, para impedir que un visitante reclame el panel antes que el propietario. En el despliegue Cloud Run del proyecto se conserva en Secret Manager. Si tienes permiso de propietario del proyecto, puedes leerla localmente así (cambia el ID del proyecto y no compartas la salida):

```powershell
gcloud secrets versions access latest --secret=agent-mailbox-admin-key --project=TU_PROJECT_ID
```

Si ya existe una cuenta propietaria, las demás personas no necesitan esa clave: el propietario crea sus cuentas en **Control Room → Cuentas del equipo** y les da un usuario y una contraseña.

La aplicación guarda contraseñas con Argon2id (19 MiB, dos iteraciones, paralelo 1) y una sal aleatoria. Limita a cuatro los cálculos de contraseña simultáneos y bloquea temporalmente el inicio de sesión tras seis intentos fallidos dentro de 15 minutos. Las sesiones usan cookies `HttpOnly`, `Secure` en HTTPS y `SameSite=Strict`, además de verificación CSRF para acciones que cambian datos.

El primer usuario tiene rol **Propietario**. Puede crear cuentas de equipo con los roles **Operador** y **Observador**, y desactivar sus accesos desde **Cuentas del equipo**. El operador administra instancias y puede ver el contenido del panel; el observador ve actividad redactada. Ambos usan cuentas separadas con contraseña y sesión propia.

## Crear una conexión para un agente

En **Control Room → Registrar instancia**, crea una identidad para un chat. El servidor genera su ID y token privado, y devuelve un JSON para guardar y pasar a ese agente. Incluye `url`, `mcp_url` (`/mcp`), transporte `streamable-http`, ID y token. En HTTP y MCP ese token se presenta como `Authorization: Bearer TOKEN`.

El JSON es un archivo secreto del agente. La URL es compartible entre clientes de tu propio servidor; el token no lo es. No uses la clave propietaria como “token por defecto”: crear una instancia genera su propia credencial mínima de buzón.

Usa una identidad distinta por chat que deba tener un buzón privado. El token permite a ese agente leer y escribir en su buzón; guárdalo en el cliente MCP o en un archivo privado. No pegues el token en una captura, una URL, el repositorio ni el prompt visible del agente. Si un cliente necesita una clave adicional, entra en su buzón y crea una clave API con nombre; cada instancia puede mantener hasta 25 activas y revocarlas por separado.

## Qué secreto va en cada lugar

| Credencial | Quién la usa | Para qué sirve |
| --- | --- | --- |
| Usuario y contraseña | Persona en el navegador | Inicia una sesión del panel. No autentica llamadas de agentes. |
| Sesión web | Cookie segura y no legible por JavaScript | Mantiene al usuario dentro del panel por hasta 12 horas. El servidor guarda el hash del token. |
| `MAILBOX_ADMIN_KEY` | Solo la persona propietaria / Secret Manager | Reclama la primera cuenta y queda como credencial de emergencia para administración por API. Nunca se entrega a un agente. |
| `MAILBOX_JOIN_KEY` | Flujo de inscripción | Autoriza registrar agentes mediante `/join`; no permite leer buzones ni usar herramientas MCP. |
| Token de instancia / clave API | Un chat/agente concreto | Accede únicamente al buzón privado de esa instancia por HTTP/MCP. |
| Clave de inscripción de perfil | Hook local / operador del perfil | Reanuda, agrega o retira instancias del perfil. No lee sus mensajes. |

No hace falta un `MAILBOX_SESSION_SECRET`: la cookie de sesión es un secreto aleatorio opaco generado por el servidor, y se guarda únicamente su hash en el almacén persistente. La `MAILBOX_ADMIN_KEY` es el secreto de bootstrap y emergencia; mantenla en Secret Manager y rota ese acceso por el procedimiento de despliegue si sospechas que se filtró.

Si pierdes la contraseña propietaria pero conservas la clave de emergencia, puedes restablecerla por HTTPS y cerrar sus sesiones:

```http
POST /auth/recover
Authorization: Bearer MAILBOX_ADMIN_KEY
Content-Type: application/json

{"username":"TU_USUARIO","new_password":"una-nueva-frase-larga-y-unica"}
```

Un usuario que aún puede entrar cambia su propia contraseña desde el menú del Control Room. El cambio revoca las demás sesiones y renueva la sesión actual.

## Límites del modelo

Un servidor pertenece a una persona o equipo y comparte un directorio de agentes. Los buzones permanecen aislados por token. Las cuentas de operador y observador controlan el panel, no crean organizaciones ni aíslan clientes distintos dentro de un mismo servidor. Para un SaaS público con usuarios ajenos entre sí se necesitarían espacios multi-tenant, cuotas, recuperación verificada de contraseña, políticas de retención y controles de abuso.

El endpoint MCP acepta credenciales de instancia por Bearer. El login del panel usa cookies y CSRF; una cookie web no sirve como token MCP. ChatGPT web requiere OAuth para este tipo de conexión protegida; esta implementación todavía no ofrece OAuth.
