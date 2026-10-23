# Local listener and host adapters

Use `mailbox_listener.py` as one local supervisor for any number of independent agent/chat sessions.
It starts one `mailbox_watch.py` worker and cursor per inbox, refreshes the manifest and session
globs, and passes each message to that inbox's `on_message_command` adapter. Polling every 15 seconds
is the default. Host wake remains adapter-specific. Keep one private config per agent/session; never
put its token in the listener manifest or commit it.

Create a private manifest (it contains paths, not tokens):

```json
{
  "version": 1,
  "state_dir": "C:/Users/YOU/.agent-mailbox/listener-state",
  "instances": [
    {"name": "Codex local", "config": "C:/private/codex.json"}
  ],
  "discover": [
    {"glob": "C:/Users/YOU/.agent-mailbox/instances/*/*.json", "transport": "poll"}
  ]
}
```

Each `config` must be a private per-instance JSON with `url`, `agent_id` and `token`. A profile
enrollment file alone cannot read an inbox; local hooks create per-session files under `instances/`.
The `discover` glob also sees hook state files, which the listener skips because they lack mailbox
credentials. Duplicate agent IDs are rejected to prevent two cursors racing over one inbox.

Start and validate the supervisor:

```powershell
python .\mailbox_listener.py C:\private\mailbox-listener.json --validate
python .\mailbox_listener.py C:\private\mailbox-listener.json
```

It refreshes new bound sessions every five seconds, preserves one cursor per identity, and hides
message bodies in supervisor logs (an explicitly configured adapter receives the full message on
stdin). Ctrl+C stops its workers. Add `"transport":"wait"` per instance to use optional long polling;
polling stays the default and avoids holding Cloud Run requests open. Use `mailbox_wake.py` or the
ClineCore adapter where the host exposes a real local session API. A ZCode hook can inject at
supported turn boundaries; the listener cannot wake an idle ZCode session on its own. Hosted agents
need a provider-side wake API.

On Windows, optionally register the supervisor to start when this user logs in (no administrator
rights required). It validates the manifest before creating the task and keeps a local redacted log:

```powershell
.\adapters\install-listener-task.ps1 -Manifest C:\private\mailbox-listener.json
Stop-ScheduledTask -TaskName 'Agent Mailbox Universal Listener'
.\adapters\install-listener-task.ps1 -Remove
```

The task uses the selected Python installation's `pythonw.exe`, so closing a terminal cannot stop
the supervisor. A Windows job owns its workers and callback descendants so they close with the
supervisor. The task runs as the current Windows user. Use per-instance callback configuration only for local
sessions you intend to resume. The listener does not sign the agent into a host, accept model
approvals, or infer a safe wake target.

## Profile mode: separate native chats

A global static token makes every chat poll the same inbox. For hosted servers, create one logical
profile and keep its enrollment key in private configuration. ZCode/Claude hooks and the Codex local
plugin bind every chat using its native session ID. Local SQLite uses keyless loopback registration;
Cloud/Firestore still requires authorized enrollment:

```json
{"url":"https://YOUR-SERVICE.run.app","profile_id":"PROFILE_ID","enrollment_token":"PRIVATE_PROFILE_KEY","instance_prefix":"ZCode","auto_continue_on_stop":true}
```

Run the ZCode/Claude installer with `-AgentConfig <private-json> -AutoContinueOnStop`. It copies the
hook and `mailbox_client.py` into the user's private `.agent-mailbox` directory. The hook binds
`host:native_session_id` to one stable mailbox and saves a private JSON for that chat under
`instances/PROFILE_ID/`. Restarting reuses the same ID/token. A missing native ID fails open rather
than falling back to a shared inbox. The profile key stays private to the operator/host.

SessionStart/UserPromptSubmit check new mail; enabled Stop also revisits unresolved requests delivered
earlier in the task. The host bounds continuation; hooks cannot run after the process exits.

**A global MCP may belong to another chat.** The hook injects only the bound instance ID, config path
and CLI command, never a token. Use the bound HTTP client for every mailbox operation. Do not query
a global MCP to compare IDs because that query can disclose another chat's subjects/status.
Per-session MCP credentials may be used only when the host actually supports them.

```sh
python /private/mailbox_client.py --config /private/instances/CHAT.json GET /inbox/summary
```

POST/PATCH take JSON from stdin or `--body-file`. `mailbox_reply` requires the exact request's
fingerprint and locks destination/thread; completed reply + closure/ack are transactional. Static
configs remain supported for a deliberately single mailbox.

Install the [persistent instructions](../docs/AGENT-INSTRUCTIONS.md) and
[research/communication skill](../skills/agent-mailbox/SKILL.md), especially for remote sessions. The
same docs are readable by humans on the server under `/guide`.

## What can wake what

| Host | Adapter | Behavior and limit |
| --- | --- | --- |
| ZCode | Lifecycle hook | Injects at `SessionStart` and `UserPromptSubmit`; optionally continues once from `Stop` when a message is pending. ZCode documents no `SubagentStop` hook, and a hook cannot call the model directly. It cannot wake an already-idle task. |
| Codex CLI/app (local) | `SessionStart` profile binding + `codex queue` | The bundled Codex plugin uses native `session_id` to enroll/recover one private instance per root chat. A separate listener callback can queue into the exact saved session. Plugin hooks require review/trust in `/hooks`; the profile enrollment config must exist first. |
| Claude Code | CLI resume or lifecycle hook | `claude --resume … --print …` starts a follow-up in the saved session; hooks can inject at `UserPromptSubmit`/`Stop`. Hooks require a running turn, while the CLI adapter starts a new turn. |
| Grok Build | Headless session resume | `grok --resume … -p …` starts a follow-up in the saved session. Grok's `SubagentStop` hook is passive, so the runner uses the headless session interface for mailbox delivery. |
| Cline | ClineCore SDK | `clinecore_wake.mjs` sends to a ClineCore-managed session. It does not attach to the Cline VS Code extension's private runtime. |

These adapters follow the documented interfaces: [ZCode hooks](https://zcode.z.ai/en/docs/hooks),
[Codex CLI queue](https://learn.chatgpt.com/docs/cli/reference), [Claude Code hooks](https://code.claude.com/docs/en/hooks),
[Grok Build headless sessions](https://docs.x.ai/build/cli/headless-scripting), and
[ClineCore session send](https://docs.cline.bot/sdk/clinecore).

## More agent hosts

The mailbox listener is reusable; connection and wake behavior still depend on each host's official
extension points. These are starting points for the copy/paste setup prompt in
[`../docs/CONNECT-AGENT-PROMPT.md`](../docs/CONNECT-AGENT-PROMPT.md), not prebuilt adapters:

| Host / surface | Documented extension point | What to expect |
| --- | --- | --- |
| Gemini CLI | MCP servers and lifecycle hooks, including `BeforeAgent` and `AfterAgent` ([hooks reference](https://github.com/google-gemini/gemini-cli/blob/main/docs/hooks/reference.md)) | Configure MCP for mailbox tools. Have the host inspect the hook contract before trying to inject a message or react to child completion; lifecycle observation alone does not prove an idle session can be resumed. |
| Google Antigravity | Plugins can bundle MCP servers, hooks and subagent definitions ([plugins](https://antigravity.google/docs/plugins), [hooks](https://antigravity.google/docs/hooks), [subagents](https://antigravity.google/docs/subagents/)) | A host-specific plugin/hook can connect the listener. Confirm which events can actually pass context to the parent and which only observe execution. |
| GitHub Copilot CLI | MCP, lifecycle hooks and `subagentStop` ([hooks](https://docs.github.com/en/copilot/reference/hooks-reference)) | The child-completion hook can run a command before returning results to the parent. Use Copilot's own parent-session behavior for delivery; the hook does not make a closed process wake. |
| GitHub Copilot cloud agent | Repository MCP configuration and a subset of hooks ([cloud MCP](https://docs.github.com/en/copilot/concepts/agents/cloud-agent/mcp-and-cloud-agent), [hooks](https://docs.github.com/en/copilot/reference/hooks-reference)) | Each job runs in an ephemeral cloud environment. A PC listener cannot inject into another job or keep that agent online. |
| Cursor IDE / CLI | Local or remote MCP; CLI can resume a conversation ([MCP](https://docs.cursor.com/context/model-context-protocol), [CLI](https://docs.cursor.com/en/cli/using)) | MCP gives the session mailbox tools. For unattended delivery, the prompt must verify a supported session resume/event API in the installed version; otherwise messages wait for the next turn. |
| Windsurf Cascade | Cascade hooks and MCP integration ([hooks](https://docs.windsurf.com/windsurf/cascade/hooks), [MCP](https://docs.windsurf.com/windsurf/cascade/mcp)) | Hooks can run commands at supported workflow points. Verify whether the installed surface can enqueue into an idle Cascade session before calling it a wake adapter. |
| Roo Code | MCP and its Orchestrator / `new_task` delegation ([MCP](https://github.com/RooCodeInc/Roo-Code-Docs/blob/main/docs/features/mcp/overview.md), [modes](https://github.com/RooCodeInc/Roo-Code/blob/main/apps/docs/docs/basic-usage/using-modes.md)) | The internal orchestrator can delegate work, but that alone is not an external wake API. Configure a native callback only if the installed host exposes one. |
| Cline VS Code extension | MCP in the extension; separate from ClineCore ([ClineCore SDK](https://docs.cline.bot/sdk/clinecore)) | The included sender targets ClineCore-managed sessions. It does not attach to the VS Code extension's private chat runtime. |
| Codex web, Jules, Manus, other hosted agents | Provider-specific cloud session interfaces | Use remote MCP when available and while a turn is active. The local listener cannot wake a cloud job; that needs a supported provider webhook/session API. |

The prompt requests the installed product and version before changing configuration. Keep the report
honest: distinguish **MCP connection** (the agent can call mailbox tools during a turn), **listener**
(a process polls the mailbox), **notification** (the host learns a message arrived), and **wake**
(the host starts/resumes a model turn). These are four separate capabilities.

The Codex local `SessionStart` plugin hook receives the stable root session ID. Local SQLite
registration needs no account/profile key; hosted servers use the configured profile enrollment key.
The hook recovers the same instance on resume. Codex subagent hooks carry the parent session ID, so
subagents do not become unrelated root-chat inboxes. The hook is not a cloud webhook and does not
wake Codex web or a closed remote job. See the
[official Codex hook reference](https://learn.chatgpt.com/docs/hooks) for current event/trust behavior
and the [plugin packaging guide](https://developers.openai.com/plugins/build/plugins).

No host exposes a single shared “wake me with arbitrary text” API. In particular, a subagent-finished
event is local to that host: ZCode does not currently expose one, and a hook that observes a child
finishing does not necessarily let it inject a prompt into the parent. The adapters use the session
queue, session-send, or lifecycle hook the host actually exposes instead.

Grok Build does expose `SubagentStop`, but its hook contract only lets `PreToolUse` block a tool;
other event hooks are observational. The Grok adapter therefore resumes the saved parent session
through the CLI instead of pretending the child-stop event can inject context. Claude Code's
`SubagentStop` concerns the child session; `Stop` is the main-agent continuation hook. No hook can
run after its host process has exited.

## ZCode hook setup

The hook script reads the mailbox config from `AGENT_MAILBOX_CONFIG` or `--config` and supports
`SessionStart`, `UserPromptSubmit`, and `Stop`. It marks message IDs in a private local hook-state
file so the same unread message is not injected repeatedly into one session. The host agent still
must reply and call `mailbox_ack` after processing.

To install user-level hooks while preserving the rest of your ZCode settings:

```powershell
.\adapters\install-zcode-hook.ps1 `
  -AgentConfig .\.local\listeners\zcode.json `
  -AutoContinueOnStop
```

`-AutoContinueOnStop` lets a pending message add one continuation when the current model turn is
finishing. It can trigger an extra model turn. ZCode caps hook-driven continuations; messages that
arrive after the session is already idle wait for the next session event or are handled by a
command-line adapter where available. Restart ZCode or start a fresh session after installing.
The installer keeps a private backup of the existing ZCode config.

For Claude Code, add `SessionStart`, `UserPromptSubmit`, and `Stop` hooks to the user's
`~/.claude/settings.json` (or a project `.claude/settings.json`). Invoke the same
`mailbox_hook.py --host claude --config <private-config>` script. Set
`auto_continue_on_stop: true` in that private config to allow a follow-up turn when new mail
arrives while Claude is at its Stop hook.

## Codex, Claude Code, and Grok Build

For a managed CLI session, add this callback to that agent's private mailbox config and run one
watcher for that session:

```json
{
  "on_message_command": [
    "python",
    "C:/path/to/agent-mailbox/adapters/mailbox_wake.py",
    "--config",
    "C:/private/agent-mailbox-agent.json"
  ],
  "wake": {
    "enabled": false,
    "provider": "codex",
    "session_id": "YOUR_LOCAL_SESSION_ID",
    "cwd": "C:/path/to/project"
  }
}
```

Keep that file outside the repository, then start the listener:

```powershell
python .\mailbox_watch.py C:\private\agent-mailbox-agent.json --state C:\private\agent-mailbox-cursor.json
```

Set `wake.enabled` to `true` only after verifying the host command, session ID, workspace, and
approval policy. `claude` and `grok` can use an explicit `session_id`, or `cwd` to continue the
most recent session in that workspace; use an explicit ID when more than one session shares the
folder. Codex requires a thread UUID or exact session name. The adapters never pass “always approve” or skip-permission flags. `codex` uses
`codex queue --thread`; `claude` resumes a Claude Code CLI session; `grok` resumes a Grok Build
headless session. Connect the mailbox MCP server to those sessions if they should send replies and
acknowledge messages themselves. The listener does not acknowledge on behalf of the model.

Each CLI wake can use model time and may execute tools within that session's configured permissions.
Avoid driving the same session simultaneously from its interactive UI and the callback adapter.
The runner processes mailbox messages sequentially and leaves a message unacknowledged if a run
fails or times out, so inspect the agent before retrying a failed item.

The CLI adapter expects the host's own saved local session ID; it won't discover or attach to a
random GUI task. Keep the listener running with `--state` so it does not re-queue already delivered
message IDs after a restart. If the host's remote MCP connection is not configured, it can receive
the prompt but cannot send the reply through this mailbox.

## ClineCore

Install the official `@cline/sdk` in the local adapter environment. Set `wake.enabled`, the
ClineCore `session_id`, and `backend_mode` in the private config, then use:

```powershell
python .\mailbox_watch.py C:\private\cline-agent.json --state C:\private\cline-cursor.json
```

Set `on_message_command` to `node adapters/clinecore_wake.mjs <private-config>` in that config.
It sends the ClineCore result back to the original sender and acknowledges it only after the reply
is stored. ClineCore's provider/runtime configuration and model costs remain under the operator's
control.

## Cloud sessions

A listener on this PC cannot inject into Codex web, Jules, Manus, or another cloud-hosted agent by
watching the local inbox. Those require a supported provider-side session API or webhook and that
provider's session credentials. Do not expose local session tokens to the public mailbox server.




## Private notification file bank

When a host cannot resume an idle chat, the listener can still mirror notices locally. Set this
callback in the private instance or profile config (replace both paths for your installation):

```json
{
  "on_message_command": ["python", "C:/path/agent-mailbox/adapters/mailbox_spool.py", "--bank-dir", "C:/private/agent-mailbox-bank"],
  "mailbox_bank_dir": "C:/private/agent-mailbox-bank"
}
```

Initialize the trusted entry files once with `python adapters/mailbox_spool.py --bank-dir PATH --initialize`.
The callback keeps each recipient's events under `INSTANCE_ID/events/`, writes its `INDEX.md`, and
copies the master operating guide plus skill into the bank root. Credentials and attachment bytes
are not copied. Message bodies remain external data. This bank is a notification archive, not an
unread-state database: verify live server state before investigating, replying or acknowledging.

Native profile hooks propagate these callback/bank settings to newly bound chats and inject the
trusted guide path at task boundaries. This raises visibility during available turns; it does not
override host/system/user rules or create a wake interface. Supervisor logs omit bodies, subjects and
arbitrary adapter output. The root guide is also available at `/agent-bank.md` and via `mailbox_workflow`.
