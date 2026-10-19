"""Run one universal local listener over multiple private Agent Mailbox instances.

The manifest contains paths, never agent tokens. Each worker keeps its own unread
cursor and invokes the host-specific on_message_command from its private config.
"""

import argparse
import glob
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path


def absolute(value, base):
    path = Path(os.path.expandvars(str(value))).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def read_instance(path):
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    for key in ("agent_id", "token", "url"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError(f"instance config {path} lacks {key}")
    if not data["url"].startswith("https://"):
        raise ValueError(f"instance config {path} must use HTTPS")
    return data


def load_manifest(manifest_path):
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if manifest.get("version") != 1:
        raise ValueError("listener manifest version must be 1")
    base = manifest_path.parent
    state_dir = absolute(manifest.get("state_dir", ".mailbox-listener-state"), base)
    items = [(item, False) for item in manifest.get("instances", [])]
    for discovery in manifest.get("discover", []):
        pattern = str(discovery.get("glob", ""))
        if not pattern:
            raise ValueError("each discover entry needs a glob")
        glob_path = Path(os.path.expandvars(pattern)).expanduser()
        if not glob_path.is_absolute():
            glob_path = base / glob_path
        for found in sorted(glob.glob(str(glob_path), recursive=True)):
            items.append(({**discovery, "config": found,
                           "state_dir": discovery.get("state_dir", str(state_dir))}, True))

    specs, ids, paths = {}, {}, set()
    for item, is_discovered in items:
        if not isinstance(item, dict) or not item.get("config"):
            raise ValueError("each instance needs a private config path")
        config_path = absolute(item["config"], base)
        if not config_path.is_file():
            if is_discovered:
                continue
            raise ValueError(f"instance config not found: {config_path}")
        if str(config_path).casefold() in paths:
            continue
        try:
            config = read_instance(config_path)
        except (OSError, ValueError, json.JSONDecodeError):
            if is_discovered:
                # Globs can include listener cursor files alongside bound instance configs.
                continue
            raise
        agent_id = config["agent_id"]
        if agent_id in ids and ids[agent_id] != config_path:
            raise ValueError(f"mailbox {agent_id} appears more than once; use one listener per inbox")
        interval = int(item.get("interval", 15))
        transport = item.get("transport", config.get("transport", "poll"))
        if not 5 <= interval <= 300 or transport not in ("poll", "wait"):
            raise ValueError(f"invalid transport/interval for listener {item.get('name', config_path.stem)}")
        worker_state_dir = absolute(item.get("state_dir", state_dir), base)
        state_path = absolute(item["state"], base) if item.get("state") else worker_state_dir / (
            hashlib.sha256(agent_id.encode()).hexdigest()[:24] + ".json")
        name = str(item.get("name") or config.get("instance_name") or config_path.stem)
        metadata = config_path.stat()
        key = (str(config_path), metadata.st_mtime_ns, metadata.st_size,
               str(state_path), interval, transport, name)
        specs[key] = {"name": name, "config": config_path, "state": state_path,
                      "agent_id": agent_id, "interval": interval, "transport": transport}
        ids[agent_id], paths = config_path, paths | {str(config_path).casefold()}
    return specs


def emit(event, **fields):
    print(json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      "type": event, **fields}, ensure_ascii=False), flush=True)


def drain_output(process, spec):
    try:
        for line in process.stdout:
            line = line.rstrip("\r\n")
            try:
                event = json.loads(line)
            except ValueError:
                emit("listener_worker_output", instance=spec["name"], detail="Non-JSON adapter output suppressed")
                continue
            if event.get("type") == "mailbox_message":
                message = event.get("message", {})
                # Never copy message bodies or attachment contents into supervisor logs.
                emit("mailbox_message", instance=spec["name"], agent_id=spec["agent_id"],
                     message_id=message.get("id"), from_agent_id=message.get("from_agent_id"),
                     attachment_count=message.get("attachment_count", len(message.get("attachments", []))))
            else:
                emit(event.pop("type", "listener_worker_event"), instance=spec["name"], **event)
    except (OSError, ValueError):
        return


def start_worker(spec, watcher_path):
    command = [sys.executable, str(watcher_path), str(spec["config"]),
               "--interval", str(spec["interval"]), "--state", str(spec["state"]),
               "--transport", spec["transport"]]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    process = subprocess.Popen(command, cwd=watcher_path.parent, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                               bufsize=1, creationflags=flags)
    threading.Thread(target=drain_output, args=(process, spec), daemon=True).start()
    emit("listener_worker_started", instance=spec["name"], agent_id=spec["agent_id"],
         transport=spec["transport"], interval_seconds=spec["interval"])
    return process


def stop_workers(workers):
    for worker in workers.values():
        if worker["process"].poll() is None:
            worker["process"].terminate()
    deadline = time.monotonic() + 5
    for worker in workers.values():
        process = worker["process"]
        if process.poll() is None:
            try:
                process.wait(max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                process.kill()
        process.stdout.close()


def main():
    parser = argparse.ArgumentParser(description="Supervise independent listeners for any local agent/session adapters.")
    parser.add_argument("manifest", help="Private/local JSON manifest containing instance config paths")
    parser.add_argument("--refresh", type=int, default=5, help="Seconds between manifest/glob refreshes")
    parser.add_argument("--validate", action="store_true", help="Validate without starting listeners")
    args = parser.parse_args()
    if not 1 <= args.refresh <= 300:
        parser.error("--refresh must be 1–300 seconds")
    manifest_path = Path(args.manifest).expanduser().resolve()
    specs = load_manifest(manifest_path)
    if args.validate:
        emit("listener_manifest_valid", instances=[{"name": row["name"], "agent_id": row["agent_id"],
                                                    "transport": row["transport"]}
                                                   for row in specs.values()])
        return 0
    if not specs:
        parser.error("manifest has no valid instance configs")
    watcher_path = Path(__file__).with_name("mailbox_watch.py").resolve()
    workers = {}
    failed = set()
    stop = threading.Event()
    announced_idle = False

    def request_stop(signum, frame):
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)
    emit("universal_listener_started", manifest=str(manifest_path), instances=len(specs),
         refresh_seconds=args.refresh)
    try:
        while not stop.is_set():
            try:
                specs = load_manifest(manifest_path)
            except (OSError, ValueError, json.JSONDecodeError) as error:
                emit("listener_manifest_error", error=f"{type(error).__name__}: {error}")
                stop.wait(args.refresh)
                continue
            for key in list(workers):
                worker = workers[key]
                if key not in specs or worker["process"].poll() is not None:
                    spec = worker["spec"]
                    if worker["process"].poll() is not None:
                        emit("listener_worker_stopped", instance=spec["name"],
                             agent_id=spec["agent_id"], exit_code=worker["process"].returncode)
                        failed.add(key)
                    stop_workers({key: worker})
                    del workers[key]
            for key, spec in specs.items():
                if key not in workers and key not in failed:
                    workers[key] = {"spec": spec, "process": start_worker(spec, watcher_path)}
            if not workers:
                if not announced_idle:
                    emit("listener_idle", detail="No matching instance configs; waiting for manifest/glob changes")
                    announced_idle = True
            else:
                announced_idle = False
            failed.intersection_update(specs.keys())
            stop.wait(args.refresh)
    finally:
        stop_workers(workers)
        emit("universal_listener_stopped", workers=len(workers))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
