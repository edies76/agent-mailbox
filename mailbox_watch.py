"""Portable 15-second inbox listener for any Agent Mailbox client."""

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


def request_json(url, token, method="GET", payload=None, timeout=12):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        **({"Content-Type": "application/json; charset=utf-8"} if data is not None else {}),
    })
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def save_cursor(path, cursor):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps({"after_id": cursor}), encoding="utf-8")
    os.replace(temporary, path)


def process_message(message, token, config):
    encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
    command = config.get("on_message_command")
    if command:
        if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
            raise ValueError("on_message_command debe ser una lista de argumentos de texto.")
        environment = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        result = subprocess.run(command, input=encoded, text=True, encoding="utf-8", check=False, env=environment)
        if result.returncode != 0:
            raise RuntimeError(f"El comando receptor terminó con código {result.returncode}.")
        if config.get("ack_after_callback", False):
            request_json(f"{config['url'].rstrip('/')}/inbox/{message['id']}/ack", token,
                         method="POST", payload={})
    else:
        print(json.dumps({"type": "mailbox_message", "message": message}, ensure_ascii=False), flush=True)


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Consulta un buzón cada 15 segundos y emite los mensajes nuevos.")
    parser.add_argument("config", help="JSON privado con url, agent_id y token")
    parser.add_argument("--interval", type=int, default=15, help="segundos entre consultas (por defecto: 15)")
    parser.add_argument("--state", help="archivo privado para recordar el último mensaje procesado")
    parser.add_argument("--transport", choices=("poll", "wait"), help="poll por defecto; wait activa conexión de eventos")
    args = parser.parse_args()
    if not 5 <= args.interval <= 300:
        parser.error("--interval debe estar entre 5 y 300 segundos")

    config_path = Path(args.config).expanduser().resolve()
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    for key in ("url", "agent_id", "token"):
        if not isinstance(config.get(key), str) or not config[key].strip():
            parser.error(f"Falta el campo {key!r} en el archivo de configuración")
    if not config["url"].startswith("https://"):
        parser.error("La URL del buzón debe usar HTTPS")

    state_path = Path(args.state).expanduser().resolve() if args.state else None
    if state_path:
        state_path.parent.mkdir(parents=True, exist_ok=True)
    if state_path and state_path.exists():
        cursor = int(json.loads(state_path.read_text(encoding="utf-8")).get("after_id", 0))
    else:
        cursor = 0
        if state_path:
            save_cursor(state_path, cursor)

    token = config["token"].strip()
    transport = args.transport or config.get("transport", "poll")
    if transport not in ("poll", "wait"):
        parser.error("transport debe ser poll o wait")
    base_url = config["url"].rstrip("/")
    print(json.dumps({"type": "mailbox_listener_started", "agent_id": config["agent_id"],
                      "interval_seconds": args.interval, "transport": transport}), flush=True)

    try:
        while True:
            started = time.monotonic()
            after_id = cursor
            failed = False
            try:
                while True:
                    query = urlencode({"unread_only": "true", "after_id": after_id, "limit": 200})
                    page = request_json(f"{base_url}/inbox?{query}", token)
                    messages = page.get("messages", [])
                    if not messages:
                        next_cursor = int(page.get("next_after_id", after_id))
                        if page.get("has_more") and next_cursor > after_id:
                            cursor = after_id = next_cursor
                            if state_path:
                                save_cursor(state_path, cursor)
                            continue
                        break
                    for message in messages:
                        process_message(message, token, config)
                        cursor = after_id = int(message["id"])
                        if state_path:
                            save_cursor(state_path, cursor)
                    if not page.get("has_more"):
                        break
            except (HTTPError, URLError, TimeoutError, OSError, ValueError, RuntimeError, KeyError) as error:
                failed = True
                print(json.dumps({"type": "mailbox_listener_error", "error": str(error)}, ensure_ascii=False),
                      file=sys.stderr, flush=True)
                if isinstance(error, HTTPError) and error.code in (401, 403):
                    error.close()
                    return 2
            if transport == "wait" and not failed:
                try:
                    query = urlencode({"after_id": cursor, "timeout": 25})
                    request_json(f"{base_url}/inbox/wait?{query}", token, timeout=35)
                    continue
                except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
                    print(json.dumps({"type": "mailbox_wait_error", "error": str(error)}), file=sys.stderr, flush=True)
                    if isinstance(error, HTTPError):
                        code = error.code
                        error.close()
                        if code in (401, 403):
                            return 2
                        if code in (404, 405):
                            transport = "poll"
                    time.sleep(args.interval)
                    continue
            remaining = args.interval - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(remaining)
    except KeyboardInterrupt:
        print(json.dumps({"type": "mailbox_listener_stopped", "agent_id": config["agent_id"]}), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())

