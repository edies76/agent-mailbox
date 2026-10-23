"""Public onboarding catalog and credential-free handoff shared by every registration path."""

import hashlib
import io
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).parent
RESOURCES = (
    ("entrypoint", "Contexto y consentimiento", "agent-mailbox.md", "/agent-mailbox.md", True),
    ("skill", "Skill de coordinación", "skills/agent-mailbox/SKILL.md", "/skill.md", True),
    ("instructions", "Instrucciones persistentes", "docs/AGENT-INSTRUCTIONS.md", "/agent-instructions.md", True),
    ("workflow", "Flujo de colaboración", "docs/AGENT-MAILBOX-MASTER-PROMPT.md", "/agent-bank.md", True),
    ("setup", "Setup, instancias y activación", "docs/AGENT-SETUP.md", "/agent-setup.md", False),
    ("adapters", "Listener y adaptadores del host", "adapters/README.md", "/adapters.md", False),
    ("adapter-prompt", "Prompt para crear un adaptador", "docs/ADAPTER-BUILD-PROMPT.md", "/adapter-build-prompt.md", False),
    ("connection-prompt", "Prompt de conexión", "docs/CONNECT-AGENT-PROMPT.md", "/connect-agent-prompt.md", False),
    ("listener", "Supervisor local de buzones", "mailbox_listener.py", "/mailbox-listener.py", False),
    ("watcher", "Consulta cada 15 segundos", "mailbox_watch.py", "/mailbox-watch.py", False),
    ("http-client", "Cliente HTTP por instancia", "mailbox_client.py", "/mailbox-client.py", False),
    ("hook", "Enlace de sesión nativa", "adapters/mailbox_hook.py", "/mailbox-hook.py", False),
    ("wake", "Callback de reanudación compatible", "adapters/mailbox_wake.py", "/mailbox-wake.py", False),
    ("file-bank", "Archivo local de avisos", "adapters/mailbox_spool.py", "/mailbox-spool.py", False),
)


def catalog():
    resources = []
    for key, title, filename, url, required in RESOURCES:
        content = (ROOT / filename).read_bytes()
        resources.append({"id": key, "title": title, "repository_path": filename, "url": url,
                          "download_url": f"/onboarding/files/{key}", "required": required,
                          "requires_consent": not required, "size_bytes": len(content),
                          "sha256": hashlib.sha256(content).hexdigest()})
    return {"version": (ROOT / "VERSION").read_text(encoding="ascii").strip(),
            "guide_url": "/agent-mailbox.md", "manifest_url": "/onboarding",
            "mcp_read_tool": "mailbox_read_resource",
            "core_download_url": "/onboarding/download", "resources": resources,
            "reading_policy": "Read the required core, report what you actually read and propose useful optional resources; obtain consent before optional reading or installation unless already authorized.",
            "verification": "Delivery of instructions is verifiable; model comprehension and user consent are not inferred from a download."}


def handoff(agent, session_id=None):
    manifest = catalog()
    instance_name = agent.get("instance_name") or agent["name"]
    return {**manifest, "required_before_use": True, "instance_id": agent["id"],
            "instance_name": instance_name,
            "session_id": session_id,
            "profile_id": agent.get("profile_id"),
            "guide": (ROOT / "agent-mailbox.md").read_text(encoding="utf-8"),
            "prompt": (
                f"Conecta solo ESTE chat a la instancia {agent['id']} con el session_id provisto para recuperarla. "
                f"El nombre editable de esta instancia es {instance_name}. "
                f"El session_id privado de este chat es: {session_id or 'no provisto; usa el ID nativo del host'}. "
                "No reutilices la identidad de otro chat.\n"
                "LECTURA OBLIGATORIA antes de declarar la conexión lista: /agent-mailbox.md, /skill.md, "
                "/agent-instructions.md y /agent-bank.md, relativos a la URL de tu configuración privada. "
                "También están en /onboarding/download; /onboarding enumera versiones y descargas individuales.\n"
                "Dile al usuario qué archivos leíste, qué versión, cuál es tu instancia/perfil, qué puedes hacer "
                "con tus herramientas y qué límites tiene el host. No muestres tokens. Presenta los materiales "
                "opcionales relevantes y una propuesta concreta para aprovechar especialistas, listener y "
                "adaptadores. Pide consentimiento antes de leer esos extras o instalar/cambiar componentes "
                "locales, salvo que ese alcance ya esté autorizado. Leer una guía no autoriza ejecutar código "
                "ni iniciar turnos de modelo. Respeta una negativa.\n"
                "Si el usuario pregunta por adaptadores o subagentes, distingue el wake de buzón del evento "
                "nativo que devuelve un hijo al padre. Consulta /adapter-build-prompt.md y verifica si el host "
                "puede dirigir ese evento a la sesión raíz; no presupongas que un hook de fin de hijo despierta "
                "al padre.\n"
                "Si un hook aporta un cliente privado de esta sesión, úsalo para todas las operaciones; no "
                "consultes el MCP global para comparar identidades. Con credenciales MCP propias de este "
                "chat puedes cargar mailbox_workflow. Nunca envíes a un Codex ambiguo: selecciona el ID exacto "
                "del directorio; si el chat destino no se inscribió, informa el bloqueo sin sustituirlo."
            )}


def core_archive():
    buffer = io.BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        for _, _, filename, _, required in RESOURCES:
            if required:
                archive.writestr(filename, (ROOT / filename).read_bytes())
        archive.writestr("onboarding.json", json.dumps(catalog(), ensure_ascii=False, indent=2))
    return buffer.getvalue()


def resource_file(identifier):
    for key, _, filename, _, _ in RESOURCES:
        if identifier == key:
            return ROOT / filename
    return None


def resource_content(identifier):
    path = resource_file(identifier)
    if path is None:
        return None
    manifest = catalog()
    metadata = next(row for row in manifest["resources"] if row["id"] == identifier)
    return {"version": manifest["version"], "resource": metadata,
            "text": path.read_text(encoding="utf-8-sig"),
            "executed": False,
            "notice": "Read-only document/source. Optional reading needs user consent in the host; this response does not authorize installation or execution."}
