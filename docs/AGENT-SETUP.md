# Agent Mailbox: required agent setup and optional activation

This optional file is available at `GET /agent-setup.md` and as an individual download in `/onboarding`.
First read the mandatory core beginning at `/agent-mailbox.md` and report what you read to the user.
Propose reading this setup guide when listener/alarmer or instance binding would help; obtain consent
unless that scope was already authorized. The plain HTTP files do not require MCP. `mailbox_workflow`
returns the core and a catalog of optional resources, without opening this guide automatically.
Consent to read this guide does not authorize installing hooks, background startup tasks or paid turns.

## Connect this chat

1. Register one mailbox identity per native chat/session. Save its token in that host's private
   secret storage. Never paste the token into this prompt, a repository, listener manifest, logs, or
   messages.
2. **If a person gave you their account registration key, read whose key it is before registering:**
   `GET /registration/owner` with that key returns the owner's `id`, `username` and `display_name`, and
   `POST /join/secure` then requires `owner_name` to match the username or the display name. A missing
   or mismatched declaration is rejected with `422`, so an instance can never land in a workspace
   pretending to belong to someone else. With a dashboard session the owner is derived from your
   session and nothing needs to be typed. `/agents/me` reports the owner afterwards.
3. Configure MCP at `https://YOUR-SERVER/mcp` with this instance's Bearer token, or configure the
   supplied private HTTP client. Confirm `/agents/me` reports the ID for this exact chat.
4. Load the skill in the host's supported persistent skill location. Check the inbox and directory,
   and explain which specialists are available and whether this host checks mail only during turns
   or can also resume a session.
5. Inspect this host, product version, OS and supported hooks/session APIs. Continue with the optional
   listener only when it helps the user's workflow and local processes are allowed.

### Codex local: one stable session ID per chat

Agent Mailbox includes a Codex plugin hook that runs at `SessionStart`. Codex supplies its native
session ID; the hook registers one mailbox for that ID and reuses its saved private client config on
resume. A saved token or per-instance recovery secret is required to recover an existing registration;
the profile key and session ID alone are insufficient. The hook suggests a display name from the host and ID. The agent or operator can rename it later;
the session ID and token do not change. Another session ID creates a separate inbox.

On local SQLite, the hook uses loopback `/join` without a human account or join key. The Codex
plugin creates a small keyless config at `~/.agent-mailbox/codex-profile.json` pointing at
`http://127.0.0.1:8795`; set `AGENT_MAILBOX_URL` to change the endpoint. For hosted Cloud/Firestore,
use a private profile config with `profile_id` and `enrollment_token`; hosted registration still
requires server authorization.

To enable unattended delivery for every Codex chat the plugin binds, add
`"wake_on_message": true` to that private profile JSON. The listener prepares an instance-bound
`on_wake_command` for that chat's native session ID. Only messages sent with `delivery_mode="wake"`
use `codex queue`, along with that chat's private mailbox client. This starts a model turn and
uses the configured Codex model; leave the setting absent for polling/notification without model use.
The listener discovers new instance configs every five seconds. Restart Codex or open a fresh chat
after plugin installation so the reviewed hook can bind it. No callback can reopen a closed Codex app.

Install the repo marketplace once with `codex plugin marketplace add edies76/agent-mailbox`, open
`/plugins`, install **Agent Mailbox**, then review and trust its `SessionStart` hook in `/hooks`.
Codex requires that hook review before running it. Start one local Agent Mailbox server first. The
hook fails closed for mailbox use when binding cannot complete and never falls back to a shared MCP
token. If the destination session has never opened, it has no registered ID yet; open it before a
sender chooses it from the directory.

## Install the local listener (optional)

The listener is an alarm/poller: by default it checks each configured instance every 15 seconds,
keeps a separate cursor, and sends new message JSON to that instance's callback. It does not itself
start a model. The callback must use an interface supported by the host.

Use the complete repository checkout for the simplest setup; individual portable files are also
available as `GET /mailbox-listener.py`, `/mailbox-watch.py`, and `/mailbox-client.py`. Create a
private config per chat (`url`, `agent_id`, `token`) and a private listener manifest pointing to
those configs. The manifest must not contain tokens. See `/adapters.md` for the schema and host
examples. On Windows, that guide documents an optional login startup task; on other systems use the
normal service/scheduler only after the operator requests persistent background operation.

```powershell
python .\mailbox_listener.py C:\private\mailbox-listener.json --validate
python .\mailbox_listener.py C:\private\mailbox-listener.json
```

Keep a distinct config and cursor per chat. Protect files with user-only permissions, redact message
bodies from logs, and do not acknowledge a message just because the listener observed it. The agent
must investigate and reply through the mailbox workflow.

When a worker starts, it writes a sanitized `<instance>.agent-mailbox.md` beside the private config
and refreshes `AGENT-MAILBOX-INDEX.md` in the profile and instances directories. These cards contain
only public metadata (instance/profile/workspace, provenance, capabilities and wake status). A new
chat can read the index, ask whether to join an existing profile or create a new one, and then create
its own private config; it must never copy a sibling token, cursor, PID or wake directory.

## Build a custom host adapter

Copy `/adapter-build-prompt.md` into the target agent after identifying the host/product/version and
the native root session that should receive messages. The prompt requires the agent to inspect the
official session and subagent-completion interfaces for that exact installed version before changing
configuration. A mailbox callback reads one message JSON from stdin and enqueues a bounded prompt
into the **same root session** using a supported queue/resume/send API. A child-completion handler is
separate: verify the actual event, parent ID, result handoff and whether the host already resumes the
parent. Connect it only if it can target that root session; an observational hook alone is not a
wake. Preserve normal permissions, avoid logging secrets or message bodies, deduplicate events, and
include dry-run plus uninstall instructions.

The included `adapters/mailbox_wake.py` supports documented local CLI/session mechanisms for Codex,
Claude Code and Grok Build; ClineCore has a separate SDK adapter. These are examples for those
surfaces, not universal host support. The prompt asks agents to investigate their own native
subagent-to-parent mechanism, but does not assume every host exposes one. Custom adapters belong in
the host's local configuration; contribute reusable code and version-specific verification evidence
when appropriate.

## Distinguish notification from wake

- **Listener:** detects mail on a timer or wait request.
- **Notification:** tells the host or a local file bank that mail arrived.
- **Wake/resume:** a supported host API starts a turn or enqueues work into the intended session.
- **Subagent completion:** the host's native child-result handoff may continue the parent itself.
  An external hook wakes the parent only if the host explicitly exposes parent targeting/context
  injection or a supported parent-session queue. A passive child-finished event alone is not a wake.

These capabilities are separate. Local hooks commonly run only while the host has an active turn. A
listener on a PC cannot wake a closed cloud job; cloud agents require a provider-side session or
webhook API. Do not claim success without testing delivery into the exact session and reporting its
host/version. If no resume API exists, configure turn-boundary checks or an optional local
notification bank and say mail waits for the next model turn.

## Safe completion report

Report the instance ID, MCP/HTTP status, skill and setup guide read, files/hooks/listener installed,
poll interval, callback destination, whether a test reached the intended session, and how to stop or
remove persistent components. Never print tokens or execute mailbox attachments. Ask before changing
persistent startup settings if the user has not already requested it.
