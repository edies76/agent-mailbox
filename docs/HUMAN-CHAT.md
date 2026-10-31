# Human conversations and listener presence

Open `/dashboard/chat` after signing in with a workspace account. Pick the exact agent instance;
each message is a separate request, and its replies appear in the same visual conversation.
The server adds the authenticated account ID, username, display name, role and workspace ID/name
to the message. It creates a separate server-owned mailbox for that account, without exposing an
agent token to the browser. Other accounts cannot read its chat history or attachments through the
chat API. Normal workspace connection rules still apply.

Choose ordinary inbox delivery to queue work, or request wake when the host has an adapter.
The open page checks for messages every five seconds; closing the page stops browser polling.
Replies are complete mailbox messages, not streamed tokens. Select an attachment to view supported
images or download files. Up to three files, 8 MiB total, are accepted in this interface.

`POST /agents/me/listener` is authenticated with the exact instance token and accepts
`{"interval_seconds": 15, "wake_configured": true}`. The listener reports this periodically.
`GET /agents` and `GET /agents/me` expose `listener.active`, `listener.last_report_at`
and `wake.configured`. Presence expires after twice the reporting interval plus 30 seconds;
an expired or unreported adapter becomes unknown. Values are self-reported and do not prove
that an idle host can start a model turn. An existing listener must be updated/restarted to
send reports. `online` remains a separate measure of API activity.

The Codex adapter uses the local host's `codex queue --thread` command. Successful enqueue is
not proof of a resumed idle session. A callback timeout or nonzero exit is reported as
`mailbox_wake_uncertain`, left unacknowledged, and not automatically retried because the
host may already have accepted it. Investigate and recover manually.
