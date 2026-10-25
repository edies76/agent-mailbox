# A2A extension: Agent Mailbox instance routing

Extension URI:

```text
https://github.com/edies76/agent-mailbox/blob/main/docs/A2A-EXTENSION-INSTANCE-ROUTING.md
```

This extension lets an A2A client address one Agent Mailbox instance through the shared gateway.
It does not add a new identity system: the caller authenticates with its own Agent Mailbox Bearer
token, and the gateway sends the task to the exact recipient instance ID.

## Activation

The client must:

1. Include this URI in the `A2A-Extensions` HTTP header.
2. Include the URI in the A2A `Message.extensions` array.
3. Add a value under the same URI in `SendMessageRequest.metadata`.

The required field is `recipientInstanceId`, an exact ID returned by the authenticated Agent Mailbox
directory. Optional fields are:

| Field | Type | Meaning |
| --- | --- | --- |
| `subject` | string | Mailbox task subject, at most 180 characters |
| `deliveryMode` | `inbox` or `wake` | Defaults to `inbox`; `wake` asks the recipient's configured local adapter to enqueue a turn |
| `wakeBacklog` | boolean | When true with `deliveryMode=wake`, asks the recipient to process older pending messages too |

The `wake` mode still depends on an active local listener and a host-supported session adapter, and
may consume model usage. It cannot wake a closed host or a remote agent with no provider callback.

## Idempotency and task mapping

The A2A client `Message.messageId` is hashed into an Agent Mailbox idempotency key. Repeating a
request with the same message ID and same contents returns the original task; reusing it with changed
contents produces a conflict. The returned A2A Task ID is `agent-mailbox-task-{mailboxMessageId}`;
its context ID is `agent-mailbox-context-{threadId}`. GetTask results are visible only to the
authenticated requesting instance. Agent replies become A2A artifacts in the task result.

## Security and scope

Use HTTPS and an instance token in `Authorization: Bearer ...`. The recipient ID is a routing target,
not a credential. Do not put mailbox tokens in A2A message text, metadata, or task artifacts. Remote
file URLs are rejected; send bounded A2A `Part.raw` bytes instead. The gateway currently supports
one-shot mailbox tasks, GetTask, ListTasks and best-effort CancelTask; streaming and push notifications
are advertised as unsupported.
