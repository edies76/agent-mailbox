"""Content-bound retry identifiers: no duplicate sends after a lost HTTP response."""

import hashlib
import json


def delivery_identity(sender, body, attachments, finalize):
    if not body.idempotency_key:
        return None, None
    key = hashlib.sha256((sender + "\0" + body.idempotency_key).encode()).hexdigest()
    payload = {"recipient": body.to_agent_id, "text": body.text, "subject": body.subject,
               "in_reply_to": body.in_reply_to, "share_subject": body.share_subject,
               "message_type": body.message_type, "delivery_mode": body.delivery_mode,
               "wake_backlog": body.wake_backlog, "finalize": finalize,
               "files": [{"name": item["filename"], "type": item["media_type"],
                          "sha256": hashlib.sha256(item["data"]).hexdigest()} for item in attachments]}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return key, digest
