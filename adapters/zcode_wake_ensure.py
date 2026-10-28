# Reference copy of the ZCode wake pattern documented in docs/ZCODE-WAKE.md.
# Running copy from the author's machine, sanitized: every path and identity comes from the
# private instance config passed with --config; no tokens and no absolute user paths.
"""Dice si hay que armar el despertador o si ya hay uno escuchando.

Existe por un fallo real: rearmar a ciegas mata al despertador anterior, y cuando
una tarea en segundo plano termina (aunque sea por un kill) el host avisa al chat
y abre un turno sin correo que lo justifique. Con esto la comprobacion se hace
ANTES de lanzar nada, en primer plano, donde terminar no avisa a nadie.

Uso al cerrar un turno:
  python zcode_wake_ensure.py --config <config privada del chat>
    -> "ARMADO pid=NNN"   ya hay uno vivo: no lances nada
    -> "NO_ARMADO"        no hay ninguno: lanza zcode_wake_watch.py en segundo plano
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path


def wake_dir_of(config):
    """Carpeta de la señal de esta instancia (por instancia, nunca compartida)."""
    if config.get("wake_dir"):
        return Path(config["wake_dir"]).expanduser()
    agent = str(config.get("agent_id") or "")[:8]
    bank = config.get("mailbox_bank_dir")
    base = Path(bank).expanduser().parent if bank else Path.home() / ".agent-mailbox"
    return base / "zcode-wake" / agent if agent else base / "zcode-wake"


def alive(pid):
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True, timeout=30).stdout
        return str(pid) in out
    except (OSError, subprocess.SubprocessError):
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    config = json.loads(Path(args.config).expanduser().read_text(encoding="utf-8-sig"))
    pid_file = wake_dir_of(config) / "watch.pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pid = None
        if pid and alive(pid):
            print(f"ARMADO pid={pid}")
            return 0
    print("NO_ARMADO")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
