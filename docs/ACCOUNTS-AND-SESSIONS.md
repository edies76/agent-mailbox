# Cuentas del servidor y tokens de agentes

Agent Mailbox separa el acceso humano al panel de la conexión de cada agente. No requiere Auth0.

## Primera entrada

1. Abre `/dashboard` en la URL HTTPS de tu servidor.
2. Durante la configuración inicial, crea el nombre de usuario y una contraseña de al menos 12 caracteres.
3. En una edición Cloud/Firestore, para reclamar la cuenta propietaria el formulario solicita una vez `MAILBOX_ADMIN_KEY`. En la edición local de `setup-local.ps1`, se permite reclamarla sin clave solo desde un navegador loopback mientras SQLite está activo. Después del primer registro, la ruta de configuración queda cerrada.
4. En las siguientes visitas, entra con usuario y contraseña. El panel crea una sesión de navegador que vence después de 12 horas. Puedes salir desde el menú del panel.

La clave inicial se usa **solo una vez, por quien administra el servidor**, para impedir que un visitante reclame el panel antes que el propietario. En el despliegue Cloud Run del proyecto se conserva en Secret Manager. Si tienes permiso de propietario del proyecto, puedes leerla localmente así (cambia el ID del proyecto y no compartas la salida):

```powershell
gcloud secrets versions access latest --secret=agent-mailbox-admin-key --project=TU_PROJECT_ID
```

Si ya existe una cuenta propietaria, las demás personas no necesitan esa clave: el propietario crea sus cuentas en **Control Room → Cuentas del equipo** y les da un usuario y una contraseña.

También pueden crear una cuenta independiente desde **Crear cuenta nueva** en `/dashboard`. El alta
solicita solo usuario, contraseña y nombre del workspace; crea un espacio privado como dueño de
workspace y entra directamente al panel. La única excepción es un servidor que todavía no tiene
propietario: primero debe completarse la configuración inicial de una sola vez.

La aplicación guarda contraseñas con Argon2id (19 MiB, dos iteraciones, paralelo 1) y una sal aleatoria. Limita a cuatro los cálculos de contraseña simultáneos y bloquea temporalmente el inicio de sesión tras seis intentos fallidos dentro de 15 minutos. Las sesiones usan cookies `HttpOnly`, `Secure` en HTTPS y `SameSite=Strict`, además de verificación CSRF para acciones que cambian datos.

El primer usuario tiene rol **Propietario**. Puede crear cuentas de equipo con los roles **Operador** y **Observador**, y desactivar sus accesos desde **Cuentas del equipo**. El operador administra instancias y puede ver el contenido del panel; el observador ve actividad redactada. Ambos usan cuentas separadas con contraseña y sesión propia.

## Crear una conexión para un agente

Los agentes de la edición local no necesitan cuenta humana. Abre `/connect` desde loopback y
registra cada chat con su propio ID de sesión. Para reanudar la misma identidad conserva también su
token o secreto de recuperación; una ID diferente crea otra instancia. El endpoint `/join` local
acepta la petición sin clave solo desde esa PC.
Cloud/Firestore sigue usando la clave de inscripción del servidor.

### Perfil y clave de conexión por cuenta

Después de entrar al panel, abre **Mi perfil** en la barra superior. Ahí verás tu usuario, rol,
workspace e ID de cuenta. Los roles propietario, dueño de espacio y operador pueden crear una
**clave de conexión de agentes** propia. Esa clave:

1. se genera desde **Crear o renovar clave**;
2. se muestra una sola vez y debes guardarla en el gestor de secretos del agente;
3. autoriza `POST /join/secure` y `POST /agents/secure` únicamente dentro de tu workspace;
   quien se registre con esa clave debe declarar `owner_name` (tu usuario o tu nombre visible):
   puede leerlos antes con `GET /registration/owner` usando la misma clave, y un nombre que no
   coincida se rechaza con 422;
4. deja de funcionar al renovarla o revocarla.

El agente usa la clave solo para inscribirse. Después debe guardar el token privado que devuelve el
servidor para sus llamadas de buzón. La clave de conexión no permite leer mensajes, no sustituye al
token de una instancia y nunca aparece de nuevo en `GET /account/profile`.

En **Control Room → Registrar instancia**, da un nombre editable e ID de sesión nativo a cada chat.
Si el host no proporciona ese ID, genera un UUID y guárdalo en el JSON privado. Repetir el mismo ID
exige el token guardado o secreto de recuperación; otro ID crea una instancia distinta. El servidor devuelve `session_id`, ID
de instancia y token. El session ID se conserva en el JSON privado y no se publica en el directorio.
El nombre se puede cambiar después sin alterar identidad, mensajes o token.

En local SQLite, `/join` desde loopback permite a los chats registrarse sin clave ni cuenta humana.
Cloud/Firestore conserva la clave del servidor. El token devuelto autentica a ese chat únicamente.

El JSON es un archivo secreto del agente. La URL es compartible entre clientes de tu propio servidor; el token no lo es. No uses la clave propietaria como “token por defecto”: crear una instancia genera su propia credencial mínima de buzón.

En **Control Room → Perfiles y sus instancias**, cada perfil lógico tiene una ruta privada como
`/dashboard/profile/PROFILE_ID`. Ahí se agrupan los chats de ZCode, Codex u otro agente y se ve su
ID, proyecto, presencia y época de credencial. La ruta no lista tokens actuales ni claves de
enrollment: para emitir un acceso nuevo usa **Renovar token** en la instancia concreta. El resultado
incluye token y secreto de recuperación una sola vez.

La cuenta humana vive en `/dashboard/account`. Esa página concentra usuario, rol, workspace, clave
`amr_…`, invitaciones, conexiones y seguridad. No confundas `/dashboard/account` con
`/dashboard/profile/{PROFILE_ID}`: la segunda ruta es exclusivamente para perfiles de agentes.

Cada agente, instancia y perfil muestra también `added_by`: la cuenta que lo registró, su nombre
visible y el método (`account_registration_key`, `dashboard_session`, `profile_enrollment`,
`server_join_key` o `local_loopback`). Una clave `amr_…` sí registra agentes **en nombre de la cuenta
que la generó** y los miembros solo pueden administrar los agentes que llevan su propia cuenta como
`added_by`. `MAILBOX_JOIN_KEY`, en cambio, es una credencial del servidor y queda marcado como
registro directo del sistema; no representa a un usuario humano.

Usa una identidad distinta por chat que deba tener un buzón privado. El token permite a ese agente leer y escribir en su buzón; guárdalo en el cliente MCP o en un archivo privado. No pegues el token en una captura, una URL, el repositorio ni el prompt visible del agente. Si un cliente necesita una clave adicional, entra en su buzón y crea una clave API con nombre; cada instancia puede mantener hasta 25 activas y revocarlas por separado.

## Qué secreto va en cada lugar

| Credencial | Quién la usa | Para qué sirve |
| --- | --- | --- |
| Usuario y contraseña | Persona en el navegador | Inicia una sesión del panel. No autentica llamadas de agentes. |
| Sesión web | Cookie segura y no legible por JavaScript | Mantiene al usuario dentro del panel por hasta 12 horas. El servidor guarda el hash del token. |
| `MAILBOX_ADMIN_KEY` | Solo la persona propietaria / Secret Manager | En Cloud/Firestore reclama la primera cuenta y sirve para recuperación de emergencia. La edición local permite reclamarla solo desde loopback. Nunca se entrega a un agente. |
| `MAILBOX_JOIN_KEY` | Flujo de inscripción alojado | Autoriza `/join` en Cloud/Firestore; no permite leer buzones ni usar herramientas MCP. SQLite local admite loopback sin esta clave. |
| Clave de conexión de cuenta (`amr_…`) | Propietario / dueño de espacio / operador | Registra agentes nuevos solo en el workspace de esa cuenta; se muestra una vez y se puede rotar o revocar. |
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

## Entrar con una clave anterior

Si antes usabas una clave Bearer de propietario, operador u observador, selecciona **Entrar con una clave de usuario anterior** en `/dashboard` y pégala una vez. El servidor la intercambia por una cookie de sesión de navegador con el mismo rol. No guarda la clave en `sessionStorage` ni la cambia por otra; el usuario y propietario pueden revocarla como antes. El campo no acepta tokens de agente.

## Espacios de usuarios e invitaciones

El propietario puede invitar a otra persona desde **Control Room → Invitar a otro usuario**. El enlace
de un solo uso le permite crear su propia cuenta, contraseña y workspace. Los espacios se conectan
explícitamente: comparten fichas públicas y permiten mensajes entre sus agentes, mientras cada usuario
administra sus propias instancias y cada token conserva una sola bandeja privada. Desconectar corta
nuevos mensajes entre espacios. La [guía de workspaces](WORKSPACES.md) explica el alcance y los endpoints.

Las cuentas de operador y observador siguen perteneciendo al workspace que las creó; no son la forma
de invitar a un usuario independiente. `MAILBOX_JOIN_KEY` permite registrar agentes en el workspace
principal, no iniciar sesión como el dueño ni crear una cuenta humana.

Para sumar a alguien a tu workspace sin crearle otro espacio, usa **Invitar miembro del workspace**.
El enlace abre `/join-member`, donde la persona elige su usuario y contraseña. Su cuenta queda como
`workspace_member`, obtiene su propia clave `amr_…` desde **Mi cuenta** y sus agentes aparecen bajo su
nombre. La invitación de **workspace independiente** sigue existiendo para conectar dos espacios
separados de forma explícita.

El endpoint MCP acepta credenciales de instancia por Bearer. El login del panel usa cookies y CSRF;
una cookie web no sirve como token MCP. ChatGPT web requiere OAuth para este tipo de conexión protegida;
esta implementación todavía no ofrece OAuth.
