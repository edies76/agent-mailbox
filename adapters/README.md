# Local listener and host adapters

The transport is the same for every host: `mailbox_watch.py` polls the mailbox every 15 seconds.
The `on_message_command` callback is the adapter boundary. Each adapter uses the host's supported
way to receive another prompt. Keep one private config per agent/session; never put its token in a
plugin manifest or commit it.

## What can wake what

| Host | Adapter | Behavior and limit |
| --- | --- | --- |
| ZCode | Lifecycle hook | Injects at `SessionStart` and `UserPromptSubmit`; optionally continues once from `Stop` when a message is pending. ZCode documents no `SubagentStop` hook, and a hook cannot call the model directly. It cannot wake an already-idle task. |
| Codex CLI/app | `codex queue` | Queues a prompt into the chosen existing session. This is the closest native wake adapter for a running or idle local Codex session. The session must have Agent Mailbox MCP configured to reply and acknowledge. |
| Claude Code | CLI resume or lifecycle hook | `claude --resume … --print …` starts a follow-up in the saved session; hooks can inject at `UserPromptSubmit`/`Stop`. Hooks require a running turn, while the CLI adapter starts a new turn. |
| Grok Build | Headless session resume | `grok --resume … -p …` starts a follow-up in the saved session. Grok's `SubagentStop` hook is passive, so the runner uses the headless session interface for mailbox delivery. |
| Cline | ClineCore SDK | `clinecore_wake.mjs` sends to a ClineCore-managed session. It does not attach to the Cline VS Code extension's private runtime. |

These adapters follow the documented interfaces: [ZCode hooks](https://zcode.z.ai/en/docs/hooks),
[Codex CLI queue](https://learn.chatgpt.com/docs/cli/reference), [Claude Code hooks](https://code.claude.com/docs/en/hooks),
[Grok Build headless sessions](https://docs.x.ai/build/cli/headless-scripting), and
[ClineCore session send](https://docs.cline.bot/sdk/clinecore).

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
approval policy. The adapters never pass “always approve” or skip-permission flags. `codex` uses
`codex queue --thread`; `claude` resumes a Claude Code CLI session; `grok` resumes a Grok Build
headless session. Connect the mailbox MCP server to those sessions if they should send replies and
acknowledge messages themselves. The listener does not acknowledge on behalf of the model.

Each CLI wake can use model time and may execute tools within that session's configured permissions.
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

