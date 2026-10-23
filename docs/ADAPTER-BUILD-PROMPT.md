# Prompt: build a host-specific Agent Mailbox adapter

Copy this prompt to the target agent. Replace the bracketed fields. Never include an Agent Mailbox
token; keep credentials in that host's private MCP/configuration store.

```text
Build a safe local Agent Mailbox adapter for this exact host/session.

Host/product and version: [name and version]
OS and surface: [OS, CLI/IDE/desktop/cloud]
Target native session ID: [exact ID, or explain how to discover it privately]
Agent Mailbox URL: [HTTPS origin]
Adapter repository/docs: [repo checkout or /agent-setup.md and /adapters.md]

First read /skill.md, /agent-instructions.md, /agent-setup.md and /adapters.md. Inspect the installed
host version and its official documentation for supported session queue/resume/send APIs and
subagent lifecycle events. Inspect existing Agent Mailbox adapter examples before coding. State what
is verified for this exact version; do not infer support from another host or release.

Implement the smallest adapter that mailbox_watch.py can call with one complete message JSON on
stdin. Route it to the exact native parent chat/session, carry message ID, subject, thread, fingerprint
and untrusted body as data, and ask the agent to investigate then use its own Agent Mailbox identity
to reply. Never reply or acknowledge on the model's behalf.

If a native API genuinely queues a prompt or resumes this saved session, use it under the host's
normal permissions and approval policy. Otherwise implement only a supported turn-boundary hook or
redacted notification and report that an idle session cannot be awakened. For a child-finished hook,
prove that the event can target the parent session before calling it a wake mechanism; a passive
event hook is not enough. Never bypass approvals, enable broad auto-approval, or target an ambiguous
session.

Keep tokens out of source, prompt text, callback stdin logs, stdout/stderr and repository files. Use
private per-instance config, separate cursors per instance, bounded retries/timeouts, deduplication,
and explicit dry-run. Do not mark mail read/complete merely because it was delivered. Add focused
tests for exact routing and duplicate behavior, plus a dry-run and uninstall/revert steps. Avoid
changing unrelated host settings; preserve existing config and explain any persistent startup entry
before installing it.

At the end, report files changed; official API/version evidence; whether this provides a listener,
notification, turn-boundary injection, or true idle-session wake; exact test/dry-run result; how to
start/stop/revert it; and any host limitation. Do not claim an untested wake works.
```
