# Reference copy of the ZCode wake pattern documented in docs/ZCODE-WAKE.md.
# Running copy from the author's machine, sanitized: every path and identity comes from the
# private instance config passed with --config; no tokens and no absolute user paths.
"""Despertador del buzon para este chat de ZCode: aviso + inyeccion permanente.

Como funciona: el listener universal detecta el mensaje y llama a este script
(su ``on_wake_command``) solo para los mensajes ``delivery_mode=wake``. Aqui se
hacen tres cosas, en este orden:

1. Se archiva el mensaje: marcador en ``wake_dir\\events\\``, indice ``PENDING.md``
   y la senal generica ``wake.json`` (solo dice "alguien te escribio", sin el
   contenido).
2. **Se inyecta el aviso en la sesion viva** con el propio CLI de ZCode
   (``zcode.cjs --resume <sesion> --surface desktop -p <texto>``). Ese es el
   despertador permanente: abre un turno en el chat que ya esta abierto, sin
   tareas en segundo plano que rearmar. La sesion se lee de ``wake_dir\\session.json``,
   que el hook de ciclo de vida mantiene al dia.
3. Se lanza un aviso de Windows, por si el chat no esta en primer plano.

Sin esto, ZCode (app Electron, sin resume/queue propio) solo despertaba en el
limite de un turno. No confirma, no responde y no borra nada del buzon.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

STATUS_RECORDED = "wake-recorded"
STATUS_DUPLICATE = "duplicate"
STATUS_REJECTED = "rejected-recipient"
STATUS_NOT_WAKE = "not-a-wake"

DEFAULT_CLI = r"C:\Program Files\ZCode\resources\glm\zcode.cjs"
WAKE_TEXT = ("Despertador activo: llego correo de Agent Mailbox (mensaje {ids}). "
             "Lee la bandeja de esta instancia con su cliente privado, responde lo que pida "
             "con la huella de la peticion y termina.")

MESSAGE_FIELDS = (
    "id", "from_agent_id", "to_agent_id", "subject", "text", "thread_id",
    "request_fingerprint", "message_type", "status", "created_at",
    "created_at_iso", "delivery_mode", "wake_backlog", "attachment_count",
)


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_config(path):
    config = json.loads(Path(path).expanduser().read_text(encoding="utf-8-sig"))
    for key in ("agent_id",):
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise ValueError(f"missing or empty {key!r} in the private config")
    return config


def wake_dir_of(config):
    """Carpeta de la señal de ESTA instancia.

    Por instancia, no compartida: dos chats del mismo agente (una bifurcación, por
    ejemplo) compartirían `wake.json` y `watch.pid`, y el despertador de uno
    despertaría al otro. Comprobado el 27-10-2026 al bifurcar la sesión.
    """
    if config.get("wake_dir"):
        return Path(config["wake_dir"]).expanduser()
    agent = str(config.get("agent_id") or "")[:8]
    bank = config.get("mailbox_bank_dir")
    base = Path(bank).expanduser().parent if bank else Path.home() / ".agent-mailbox"
    return base / "zcode-wake" / agent if agent else base / "zcode-wake"


def recipient_of(message):
    for key in ("to_agent_id", "to_instance_id"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def read_live_session(wake_dir, config):
    """La sesion viva: la que el hook anota; si no, la configurada a mano."""
    path = wake_dir / "session.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("session_id"):
            return str(record["session_id"]), record.get("cwd") or ""
    except (OSError, ValueError):
        pass
    return (str(config.get("session_id")) if config.get("session_id") else ""), ""


def inject_wake(config, wake_dir, prompt):
    """Abre un turno en la sesion viva. Nunca bloquea y deja el resultado en un log.

    Delega en `zcode_inject.py` (que corre el CLI en primer plano y guarda codigo
    de salida y salida), en vez de lanzar el CLI a ciegas: el 27-10-2026 el log
    decia "inyectado" para los mensajes 224, 225 y 226 mientras el CLI en
    realidad devolvia codigo 1 por la forma de la ruta y nadie despertaba.
    """
    session_id, _cwd = read_live_session(wake_dir, config)
    if not session_id:
        return "sin-sesion"
    injector = Path(__file__).with_name("zcode_inject.py")
    if not injector.exists():
        return "sin-inyector"
    try:
        subprocess.Popen([sys.executable, str(injector), "--wake-dir", str(wake_dir), "--text", prompt],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                         | getattr(subprocess, "DETACHED_PROCESS", 0))
        return "lanzado"
    except OSError as error:
        return f"error:{type(error).__name__}"


def log_injection(wake_dir, record):
    try:
        path = wake_dir / "inject-log.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except (OSError, ValueError):
        pass


def notify_windows(title, body):
    """Best-effort Windows toast, detached so the listener's poll is never blocked."""
    if os.name != "nt":
        return False
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
        "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        "$x = $t.GetElementsByTagName('text');"
        "$x.Item(0).AppendChild($t.CreateTextNode($env:MAILBOX_WAKE_TITLE)) > $null;"
        "$x.Item(1).AppendChild($t.CreateTextNode($env:MAILBOX_WAKE_BODY)) > $null;"
        "$n = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Agent Mailbox');"
        "$n.Show([Windows.UI.Notifications.ToastNotification]::new($t));"
    )
    environment = dict(os.environ, MAILBOX_WAKE_TITLE=title, MAILBOX_WAKE_BODY=body)
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                         env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return True
    except OSError:
        return False


def render_index(entries):
    lines = ["# Wake channel — ZCode Lunar Mobile chat", "",
             "Wake-flagged messages (`delivery_mode=\"wake\"`) received by this instance.",
             "Each one is archived here, injected into the live session through the ZCode CLI",
             "and announced with a Windows notification.", ""]
    for entry in entries:
        lines.append(f"- #{entry.get('id')} · {entry.get('created_at_iso', '')} · "
                     f"{(entry.get('subject') or '(sin asunto)')[:80]} · from {entry.get('from_agent_id', '')}")
    return "\n".join(lines) + "\n"


def main():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Private config JSON of this instance")
    parser.add_argument("--no-inject", action="store_true", help="Solo archivar; no abrir turno")
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        message = json.load(sys.stdin)
        if message.get("delivery_mode") != "wake":
            print(json.dumps({"status": STATUS_NOT_WAKE, "message_id": message.get("id")}))
            return 0
        if recipient_of(message) != config["agent_id"]:
            print(json.dumps({"status": STATUS_REJECTED, "message_id": message.get("id"),
                              "recipient": recipient_of(message)}))
            return 0
        wake_dir = wake_dir_of(config)
        index_path = wake_dir / "PENDING.md"
        try:
            entries = json.loads((wake_dir / "index.json").read_text(encoding="utf-8")).get("entries", [])
        except (OSError, ValueError):
            entries = []
        if any(str(entry.get("id")) == str(message.get("id")) for entry in entries):
            print(json.dumps({"status": STATUS_DUPLICATE, "message_id": message.get("id")}))
            return 0

        record = {key: message.get(key) for key in MESSAGE_FIELDS}
        record["recorded_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        record["host"] = "zcode"
        atomic_write(wake_dir / "events" / f"{int(message['id']):020d}.json",
                     json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        entries.append(record)
        atomic_write(wake_dir / "index.json",
                     json.dumps({"entries": entries}, ensure_ascii=False, indent=2) + "\n")
        atomic_write(index_path, render_index(entries))

        prompt = WAKE_TEXT.format(ids=f"#{message['id']}")
        signal = {"generado": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "generado_epoch": time.time(), "texto": prompt,
                  "mensajes": [record.get("id")], "de": record.get("from_agent_id")}
        atomic_write(wake_dir / "wake.json", json.dumps(signal, ensure_ascii=False, indent=2) + "\n")

        injected = "desactivado" if args.no_inject else inject_wake(config, wake_dir, prompt)
        notified = notify_windows(
            "Agent Mailbox · mensaje despertador",
            f"#{record.get('id')} de {record.get('from_agent_id', '')}: "
            f"{(record.get('subject') or 'sin asunto')[:90]}")
        log_injection(wake_dir, {"time": record["recorded_at"], "message_id": record.get("id"),
                                 "injected": injected, "notified": notified})
        print(json.dumps({"status": STATUS_RECORDED, "message_id": message.get("id"),
                          "wake_dir": str(wake_dir), "injected": injected, "notified": notified}))
        return 0
    except Exception as error:  # noqa: BLE001 - the callback must report, never crash the watcher
        print(json.dumps({"status": "error", "error": type(error).__name__, "detail": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
