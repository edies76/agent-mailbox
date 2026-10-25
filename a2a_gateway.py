"""Small A2A HTTP+JSON mapping for Agent Mailbox's per-instance task inboxes."""

import base64
import re
from fastapi.responses import JSONResponse


ROUTING_EXTENSION = "https://github.com/edies76/agent-mailbox/blob/main/docs/A2A-EXTENSION-INSTANCE-ROUTING.md"
A2A_VERSION = "1.0"
TASK_PREFIX = "agent-mailbox-task-"
CONTEXT_PREFIX = "agent-mailbox-context-"
MESSAGE_PREFIX = "agent-mailbox-message-"


def agent_card(origin, app_version):
    origin = origin.rstrip("/")
    return {
        "name": "Agent Mailbox Gateway",
        "description": (
            "A2A HTTP+JSON gateway that routes one-shot tasks to an exact Agent Mailbox instance. "
            "The recipient is selected by the Agent Mailbox instance-routing extension; results are "
            "available through A2A GetTask after the mailbox agent replies."
        ),
        "supportedInterfaces": [{
            "url": origin + "/a2a",
            "protocolBinding": "HTTP+JSON",
            "protocolVersion": "1.0",
        }],
        "version": app_version,
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "extensions": [{
                "uri": ROUTING_EXTENSION,
                "description": (
                    "Routes a task to one exact Agent Mailbox instance. Activate this URI in "
                    "A2A-Extensions and provide metadata under this URI with recipientInstanceId; "
                    "optional keys are subject, deliveryMode (inbox|wake), and wakeBacklog."
                ),
                "required": True,
                "params": {
                    "metadataKey": ROUTING_EXTENSION,
                    "required": ["recipientInstanceId"],
                    "optional": {"subject": "string", "deliveryMode": ["inbox", "wake"],
                                  "wakeBacklog": "boolean"},
                },
            }],
        },
        "securitySchemes": {
            "agentMailboxBearer": {
                "httpAuthSecurityScheme": {
                    "scheme": "Bearer",
                    "description": "Use the caller's private Agent Mailbox instance token.",
                }
            }
        },
        "securityRequirements": [{"schemes": {"agentMailboxBearer": {"list": []}}}],
        "defaultInputModes": ["text/plain", "application/json", "application/octet-stream"],
        "defaultOutputModes": ["text/plain", "application/octet-stream"],
        "skills": [{
            "id": "delegate-mailbox-task",
            "name": "Delegate to an Agent Mailbox instance",
            "description": (
                "Send a one-shot request to an exact instance ID. Use the Agent Mailbox directory "
                "to find that ID. The task runs asynchronously in that instance's own inbox; poll "
                "GetTask for the final reply and artifacts. Set returnImmediately=true."
            ),
            "tags": ["agent-to-agent", "delegation", "private-inbox", "task-routing"],
            "examples": [
                "Send a bounded code review task to the instance ID I selected and return its evidence."
            ],
            "inputModes": ["text/plain", "application/json", "application/octet-stream"],
            "outputModes": ["text/plain", "application/octet-stream"],
        }],
    }


def task_id(message_id):
    return f"{TASK_PREFIX}{int(message_id)}"


def context_id(message):
    return f"{CONTEXT_PREFIX}{int(message.get('thread_id') or message['id'])}"


def mailbox_id_from_task(value):
    match = re.fullmatch(re.escape(TASK_PREFIX) + r"([1-9][0-9]*)", str(value))
    if not match:
        return None
    return int(match.group(1))


def mailbox_thread_from_context(value):
    match = re.fullmatch(re.escape(CONTEXT_PREFIX) + r"([1-9][0-9]*)", str(value))
    return int(match.group(1)) if match else None


def mailbox_thread_from_context_id(value):
    return mailbox_thread_from_context(value)


def status_state(status):
    return {
        "queued": "TASK_STATE_SUBMITTED",
        "in_progress": "TASK_STATE_WORKING",
        "completed": "TASK_STATE_COMPLETED",
        "blocked": "TASK_STATE_INPUT_REQUIRED",
        "cancelled": "TASK_STATE_CANCELED",
    }.get(status, "TASK_STATE_FAILED")


def mailbox_status_from_task_state(state):
    return {
        "TASK_STATE_SUBMITTED": "queued",
        "TASK_STATE_WORKING": "in_progress",
        "TASK_STATE_COMPLETED": "completed",
        "TASK_STATE_INPUT_REQUIRED": "blocked",
        "TASK_STATE_CANCELED": "cancelled",
    }.get(state)


def problem(status, error_name, detail):
    reason = re.sub(r"(?<!^)(?=[A-Z])", "_", error_name.removesuffix("Error")).upper()
    http_status = {"InvalidParams": 400, "TaskNotCancelable": 400, "UnsupportedOperation": 400,
                   "ContentTypeNotSupported": 400, "ExtensionSupportRequired": 400,
                   "VersionNotSupported": 400}.get(error_name.removesuffix("Error"), status)
    status_name = {400: "INVALID_ARGUMENT", 401: "UNAUTHENTICATED", 403: "PERMISSION_DENIED",
                   404: "NOT_FOUND", 409: "ABORTED", 500: "INTERNAL", 501: "UNIMPLEMENTED"}.get(
                       http_status, "UNKNOWN")
    return JSONResponse({"error": {
        "code": http_status,
        "status": status_name,
        "message": detail,
        "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                     "reason": reason, "domain": "a2a-protocol.org"}],
    }}, status_code=http_status, media_type="application/a2a+json")


def encode_page_token(message_id):
    if not message_id:
        return ""
    return base64.urlsafe_b64encode(str(int(message_id)).encode("ascii")).decode("ascii").rstrip("=")


def decode_page_token(token):
    if not token:
        return 0
    try:
        value = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode("ascii")
        number = int(value)
    except (ValueError, UnicodeDecodeError):
        raise ValueError("Invalid A2A pageToken")
    if number < 0:
        raise ValueError("Invalid A2A pageToken")
    return number


def mailbox_message(row, requester_id, a2a_task_id, a2a_context_id, attachment_reader):
    parts = []
    text = row.get("text") or ""
    if text:
        parts.append({"text": text})
    for attachment in row.get("attachments", []):
        item = attachment_reader(row["id"], attachment["index"])
        parts.append({
            "raw": base64.b64encode(item["data"]).decode("ascii"),
            "filename": item["filename"],
            "mediaType": item["media_type"],
        })
    if not parts:
        parts.append({"text": ""})
    return {
        "messageId": MESSAGE_PREFIX + str(row["id"]),
        "contextId": a2a_context_id,
        "taskId": a2a_task_id,
        "role": "ROLE_USER" if row["from_agent_id"] == requester_id else "ROLE_AGENT",
        "parts": parts,
        "extensions": [ROUTING_EXTENSION],
        "metadata": {ROUTING_EXTENSION: {
            "mailboxMessageId": row["id"],
            "mailboxInstanceId": row["to_agent_id"],
            "messageType": row.get("message_type", "request"),
            "requestFingerprint": row.get("request_fingerprint"),
            "deliveryMode": row.get("delivery_mode", "inbox"),
        }},
    }


def task_document(original, requester_id, thread, attachment_reader, history_length=50,
                  include_artifacts=True, include_history=True):
    identifier = task_id(original["id"])
    context = context_id(original)
    state = status_state(original.get("status", "queued"))
    status = {"state": state, "timestamp": original.get("updated_at_iso") or original.get("created_at_iso")}
    status_note = original.get("status_note")
    if status_note:
        status["message"] = {
            "messageId": f"{MESSAGE_PREFIX}{original['id']}-status",
            "contextId": context,
            "taskId": identifier,
            "role": "ROLE_AGENT",
            "parts": [{"text": status_note}],
        }
    result = {
        "id": identifier,
        "contextId": context,
        "status": status,
        "metadata": {ROUTING_EXTENSION: {
            "mailboxMessageId": original["id"],
            "requesterInstanceId": requester_id,
            "recipientInstanceId": original["to_agent_id"],
            "requestFingerprint": original.get("request_fingerprint"),
            "deliveryMode": original.get("delivery_mode", "inbox"),
            "wakeBacklog": bool(original.get("wake_backlog", False)),
        }},
    }
    ordered = thread.get("messages", [])
    if include_history:
        result["history"] = [mailbox_message(row, requester_id, identifier, context, attachment_reader)
                             for row in ordered[-history_length:]] if history_length > 0 else []
    if include_artifacts:
        artifacts = []
        for row in ordered:
            if row.get("message_type") != "reply" or row.get("from_agent_id") == requester_id:
                continue
            parts = []
            if row.get("text"):
                parts.append({"text": row["text"]})
            for attachment in row.get("attachments", []):
                item = attachment_reader(row["id"], attachment["index"])
                parts.append({"raw": base64.b64encode(item["data"]).decode("ascii"),
                              "filename": item["filename"], "mediaType": item["media_type"]})
            if parts:
                artifacts.append({
                    "artifactId": f"mailbox-reply-{row['id']}",
                    "name": row.get("subject") or f"Reply to {original['id']}",
                    "parts": parts,
                    "metadata": {ROUTING_EXTENSION: {
                        "mailboxMessageId": row["id"],
                        "requestFingerprint": row.get("request_fingerprint"),
                    }},
                    "extensions": [ROUTING_EXTENSION],
                })
        if artifacts:
            result["artifacts"] = artifacts
    return result
