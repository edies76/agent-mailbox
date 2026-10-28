"""Deliver one *wake-channel* Agent Mailbox message to this DeepSeek Harness chat.

Callback for mailbox_watch.py's ``on_wake_command``: the watcher calls it only
for messages the sender marked ``delivery_mode="wake"``, while ordinary inbox
mail keeps going to ``on_message_command``. This script never starts a model:
it writes a bounded, redacted marker file that the local DSH doorbell plugin
watches, and the plugin opens the turn through the host's own agent queue.

Why two channels: a mailbox where every notice woke the model would burn a turn
per message. The wake channel is an explicit sender decision, so this adapter
records only those and leaves the rest as ordinary notifications.

It never acknowledges, replies to, or deletes a mailbox message, and it makes no
network calls.
"""

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

STATUS_RECORDED = "wake-recorded"
STATUS_DUPLICATE = "duplicate"
STATUS_REJECTED = "rejected-recipient"
STATUS_NOT_WAKE = "not-a-wake"

WAKE_PREFIX = "[WAKE] "
MESSAGE_FIELDS = (
    "id", "from_agent_id", "to_agent_id", "subject", "text", "thread_id",
    "request_fingerprint", "message_type", "status", "created_at",
    "created_at_iso", "delivery_mode", "wake_backlog", "attachment_count",
)
MAX_TRACKED_IDS = 1000


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
    for key in ("url", "agent_id", "token", "session_id", "wake_dir"):
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise ValueError(f"missing or empty {key!r} in the private config")
    return config


def recipient_of(message):
    for key in ("to_agent_id", "to_instance_id"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def read_state(path):
    if not path.exists():
        return {"recorded": []}
    state = json.loads(path.read_text(encoding="utf-8"))
    return {"recorded": [item for item in state.get("recorded", []) if isinstance(item, int)]}


def deliver(message, config, dry_run=False):
    """Record one wake marker; never ack, reply, or delete anything."""
    if message.get("delivery_mode") != "wake":
        print(json.dumps({
            "type": "dsh_wake", "status": STATUS_NOT_WAKE,
            "reason": "message delivery_mode is not 'wake'; ordinary mail stays on the notice channel",
            "message_id": message.get("id"),
        }, ensure_ascii=False), flush=True)
        return STATUS_NOT_WAKE

    if recipient_of(message) != config["agent_id"]:
        print(json.dumps({
            "type": "dsh_wake", "status": STATUS_REJECTED,
            "reason": "message is addressed to another instance; not delivered",
            "message_id": message.get("id"), "expected_agent_id": config["agent_id"],
        }, ensure_ascii=False), flush=True)
        return STATUS_REJECTED

    identifier = message.get("id")
    if not isinstance(identifier, int) or identifier <= 0:
        raise ValueError("the message has no positive integer id; refusing to guess a wake name")

    wake_dir = Path(config["wake_dir"]).expanduser().resolve()
    state_path = wake_dir / "state.json"
    state = read_state(state_path)
    if identifier in state["recorded"]:
        print(json.dumps({
            "type": "dsh_wake", "status": STATUS_DUPLICATE, "message_id": identifier,
            "session_id": config["session_id"],
        }, ensure_ascii=False), flush=True)
        return STATUS_DUPLICATE

    record = {key: message[key] for key in MESSAGE_FIELDS if key in message}
    record["subject"] = WAKE_PREFIX + str(record.get("subject", ""))
    record["_wake"] = {
        "host": "deepseek-harness",
        "session_id": config["session_id"],
        "instance_id": config["agent_id"],
        "channel": "delivery_mode=wake",
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    event_path = wake_dir / "events" / f"{identifier:020d}.json"
    tracked = (state["recorded"] + [identifier])[-MAX_TRACKED_IDS:]

    if dry_run:
        print(json.dumps({
            "type": "dsh_wake", "status": STATUS_RECORDED, "dry_run": True,
            "message_id": identifier, "session_id": config["session_id"],
            "event_path": str(event_path),
        }, ensure_ascii=False), flush=True)
        return STATUS_RECORDED

    atomic_write(event_path, json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    atomic_write(state_path, json.dumps(
        {"recorded": tracked, "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
        ensure_ascii=False, indent=2) + "\n")

    print(json.dumps({
        "type": "dsh_wake", "status": STATUS_RECORDED, "message_id": identifier,
        "session_id": config["session_id"], "marker": str(event_path),
    }, ensure_ascii=False), flush=True)
    return STATUS_RECORDED


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="private JSON bound to this chat")
    parser.add_argument("--dry-run", action="store_true",
                        help="report the action without writing anything")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    message = json.load(sys.stdin)
    if not isinstance(message, dict):
        raise ValueError("stdin must carry one message JSON object")
    deliver(message, config, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
