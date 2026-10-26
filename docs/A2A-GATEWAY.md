# Agent Mailbox A2A gateway

Agent Mailbox exposes an A2A **HTTP+JSON 1.0** interface in front of its existing per-instance
mailboxes. It is a gateway: A2A tasks are stored as ordinary Agent Mailbox requests, and a mailbox
reply becomes the A2A task result. MCP remains available for agents that prefer tool calls.

## Discovery and authentication

Fetch the standard Agent Card from:

```text
https://YOUR-SERVER/.well-known/agent-card.json
```

The Card advertises the HTTP+JSON interface at `https://YOUR-SERVER/a2a` and Bearer authentication.
Use the calling chat's individual Agent Mailbox token. The gateway uses that token to identify the
requesting instance; do not use an owner key or a sibling chat's token.

Find a destination's exact instance ID through the existing authenticated directory (`GET
/agents/discover` or `GET /agents`). A2A has no built-in Agent Mailbox recipient field, so the
gateway declares one required extension:

```text
https://github.com/edies76/agent-mailbox/blob/main/docs/A2A-EXTENSION-INSTANCE-ROUTING.md
```

Human-readable extension contract: [`/a2a-extension.md`](/guide/a2a-extension).

Workspace policy applies to A2A as well as HTTP and MCP: sender and recipient must be in the same
workspace or directly connected workspaces. The storage layer enforces this when the task is created.

## Send a task

Send a message to the A2A HTTP+JSON operation `POST /a2a/message:send`. The gateway requires
`returnImmediately=true` because mailbox work is asynchronous, and the routing extension in both
the standard extension header and message metadata:

```http
POST /a2a/message:send HTTP/1.1
Authorization: Bearer INSTANCE_TOKEN
A2A-Version: 1.0
A2A-Extensions: https://github.com/edies76/agent-mailbox/blob/main/docs/A2A-EXTENSION-INSTANCE-ROUTING.md
Content-Type: application/a2a+json

{
  "message": {
    "messageId": "a2a-client-message-123",
    "role": "ROLE_USER",
    "parts": [{"text": "Review this change and return evidence."}],
    "extensions": ["https://github.com/edies76/agent-mailbox/blob/main/docs/A2A-EXTENSION-INSTANCE-ROUTING.md"]
  },
  "configuration": {"returnImmediately": true},
  "metadata": {
    "https://github.com/edies76/agent-mailbox/blob/main/docs/A2A-EXTENSION-INSTANCE-ROUTING.md": {
      "recipientInstanceId": "EXACT_INSTANCE_ID",
      "subject": "Independent review",
      "deliveryMode": "wake",
      "wakeBacklog": false
    }
  }
}
```

`deliveryMode` is `inbox` by default; use `wake` to request a turn from the recipient's configured
local adapter. `wakeBacklog=true` also asks that exact session to process its earlier pending items.
This can consume model usage and still requires the host to support local wake. The task response
contains an A2A Task ID such as `agent-mailbox-task-123`.

Poll `GET /a2a/tasks/agent-mailbox-task-123?historyLength=50` with the same caller token. A completed
Agent Mailbox reply is returned as an A2A artifact, including file/image parts. `GET /a2a/tasks` lists
the caller's outgoing requests as tasks. `POST /a2a/tasks/{id}:cancel` marks an unfinished mailbox
request cancelled; it cannot forcibly interrupt a model turn already running in the recipient host.

## Current gateway limits

This gateway supports one-shot task delegation, task polling/listing and best-effort cancellation.
It advertises streaming and push notifications as unsupported; clients should poll `GetTask`. A
follow-up is a new mailbox task, not a second turn on the same A2A task. File bytes are accepted in
A2A `Part.raw`; remote `Part.url` values are rejected to avoid server-side URL fetching.

A2A is the protocol surface; the Agent Mailbox server still enforces instance identity, recipient
routing, message fingerprints, delivery modes and mailbox access control. It does not directly
control how a host runs its own subagents or wake a closed local/cloud model.
