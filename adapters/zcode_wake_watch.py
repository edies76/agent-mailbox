# Reference copy of the ZCode wake pattern documented in docs/ZCODE-WAKE.md.
# Running copy from the author's machine, sanitized: every path and identity comes from the
# private instance config passed with --config; no tokens and no absolute user paths.
"""Despertador nativo de ZCode: espera correo en segundo plano y termina al verlo.

Por que existe: el hook Stop solo puede pedir UNA continuacion mientras el turno
del host se esta cerrando, y el host mata el proceso del hook a los pocos
segundos, asi que un mensaje que llega despues de ese instante nunca abre turno.
ZCode si sabe abrir un turno con la sesion inactiva: lo hace cuando termina una
tarea o un subagente en segundo plano. Este script usa ese mismo mecanismo:
se lanza como tarea en segundo plano, espera sin gastar turnos y, en cuanto
detecta correo nuevo de esta instancia, SALE; el host avisa al chat que la tarea
termino y el chat se reabre solo para leer la bandeja y responder.

Dos senales, de la mas rapida a la mas independiente:
1) el archivo generico del listener (``wake.json``, escrito por zcode_wake.py)
   que solo dice "alguien te escribio": se revisa cada 2 s;
2) la bandeja del servidor de esta instancia: se revisa cada 10 s, por si el
   listener no esta corriendo.

No lee adjuntos, no confirma, no responde y no borra nada.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

FAST_SECONDS = 2.0
INBOX_SECONDS = 10.0
RECYCLE_SECONDS = 12 * 3600
SEEN_LIMIT = 500
GENERIC_WAKE = ("DESPERTADOR: alguien te escribio. Lee la bandeja de esta instancia, "
                "responde lo que pida y vuelve a armar el despertador al terminar.")


def load_config(path):
    config = json.loads(Path(path).expanduser().read_text(encoding="utf-8-sig"))
    for key in ("url", "token", "agent_id"):
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise ValueError(f"missing or empty {key!r} in the private config")
    return config


def state_paths(config):
    """Carpeta propia de esta instancia: compartirla mezcla despertadores de dos chats."""
    if config.get("wake_dir"):
        base = Path(config["wake_dir"]).expanduser()
    else:
        agent = str(config.get("agent_id") or "")[:8]
        root = Path(config.get("mailbox_bank_dir") or Path.home()).expanduser().parent / "zcode-wake"
        base = root / agent if agent else root
    base.mkdir(parents=True, exist_ok=True)
    return base / "watch.pid", base / "watch-seen.json", base / "wake.json"


def pid_is_alive(pid):
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True, timeout=30).stdout
        return str(pid) in out
    except (OSError, subprocess.SubprocessError):
        return False


def claim_single_instance(pid_path, force=False):
    """Deja un solo despertador vivo por chat.

    Ojo: matar al anterior NO es gratis. Cuando una tarea en segundo plano termina
    (aunque sea por un kill), el host avisa al chat, y ese aviso abre un turno sin
    correo que lo justifique. Por eso, si ya hay uno vivo, este se retira sin tocar
    nada; el reemplazo solo con --force.
    """
    mine = os.getpid()
    if pid_path.exists():
        try:
            previous = int(pid_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            previous = 0
        if previous and previous != mine and pid_is_alive(previous):
            if not force:
                print(f"YA HAY DESPERTADOR VIVO (pid {previous}); no armo otro. "
                      "Usa --force solo si quieres reemplazarlo a propósito.", flush=True)
                return False
            try:
                subprocess.run(["taskkill", "/PID", str(previous), "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
            except (OSError, subprocess.SubprocessError):
                pass
    pid_path.write_text(str(mine), encoding="utf-8")
    return True


def load_seen(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {int(value) for value in data.get("seen", [])}
    except (OSError, ValueError):
        return set()


def save_seen(path, seen):
    keep = sorted(seen)[-SEEN_LIMIT:]
    path.write_text(json.dumps({"seen": keep}), encoding="utf-8")


def unread_messages(config):
    query = urlencode({"unread_only": "true", "after_id": 0, "limit": 50})
    request = Request(config["url"].rstrip("/") + "/inbox?" + query,
                      headers={"Authorization": "Bearer " + config["token"],
                               "Accept": "application/json"})
    with urlopen(request, timeout=15) as response:
        page = json.loads(response.read().decode("utf-8"))
    return [message for message in page.get("messages", []) if message.get("status") != "cancelled"]


def describe(messages):
    return " | ".join(
        f"#{message.get('id')} {message.get('message_type', 'request')} "
        f"de {message.get('from_agent_id', '')} "
        f"delivery={message.get('delivery_mode', 'inbox')} "
        f"huella={message.get('request_fingerprint', '')} "
        f"asunto={message.get('subject') or '(sin asunto)'}"
        for message in messages)


def local_signal(wake_path, armed_at):
    """La senal generica del listener, escrita despues de armar el despertador."""
    try:
        if not wake_path.exists():
            return None
        data = json.loads(wake_path.read_text(encoding="utf-8"))
        if float(data.get("generado_epoch") or 0) >= armed_at:
            return data
    except (OSError, ValueError):
        return None
    return None


def disarm(pid_path):
    try:
        pid_path.unlink()
    except OSError:
        pass


def main():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--force", action="store_true",
                        help="Reemplaza al despertador vivo (su muerte abre un turno sin correo)")
    args = parser.parse_args()

    config = load_config(args.config)
    pid_path, seen_path, wake_path = state_paths(config)
    if not claim_single_instance(pid_path, force=args.force):
        return 0
    seen = load_seen(seen_path)
    armed_at = time.time()
    started = time.monotonic()
    next_inbox = 0.0
    errors = 0
    print(f"DESPERTADOR ARMADO instancia={config['agent_id']} pid={os.getpid()} "
          f"senal_local_cada={FAST_SECONDS:.0f}s bandeja_cada={INBOX_SECONDS:.0f}s vistas={len(seen)}", flush=True)

    while True:
        signal = local_signal(wake_path, armed_at)
        if signal:
            print(f"{GENERIC_WAKE} (senal del listener a los {int(time.monotonic() - started)}s: "
                  f"mensajes={signal.get('mensajes')} de={signal.get('de')})", flush=True)
            disarm(pid_path)
            return 0

        if time.monotonic() >= next_inbox:
            next_inbox = time.monotonic() + INBOX_SECONDS
            try:
                messages = unread_messages(config)
                errors = 0
            except HTTPError as error:
                errors += 1
                print(f"aviso: HTTP {error.code} al consultar la bandeja (fallo {errors})", flush=True)
                messages = None
            except (URLError, TimeoutError, OSError, ValueError) as error:
                errors += 1
                print(f"aviso: sin conexion ({type(error).__name__}, fallo {errors})", flush=True)
                messages = None
            if messages is not None:
                fresh = [message for message in messages if int(message["id"]) not in seen]
                seen.update(int(message["id"]) for message in messages)
                save_seen(seen_path, seen)
                if fresh:
                    print(f"{GENERIC_WAKE} (bandeja a los {int(time.monotonic() - started)}s, "
                          f"{len(fresh)} nuevo(s): {describe(fresh)})", flush=True)
                    disarm(pid_path)
                    return 0

        if time.monotonic() - started > RECYCLE_SECONDS:
            # No se reinicia con execv: en Windows execv TERMINA el proceso actual y
            # arranca otro, el host reporta el fin de la tarea y eso abre un turno sin
            # correo que lo justifique (paso el 28-10-2026 a las 12 h de guardia).
            # Reiniciar el reloj en el sitio mantiene vivo el mismo proceso para siempre.
            started = time.monotonic()
            print(f"guardia renovada en el sitio: sigue el mismo proceso (pid {os.getpid()}), "
                  f"sin terminar y sin despertar a nadie", flush=True)
        time.sleep(FAST_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
