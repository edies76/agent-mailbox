# Contributing to Agent Mailbox

Thanks for helping make agent-to-agent work more reliable. Agent Mailbox is an open-source project for private per-chat inboxes, specialist handoffs, MCP/HTTP access, and host-specific local listeners. Useful contributions include code, docs, adapters, bug reports, security reviews, and honest compatibility testing.

## Start with a useful change

- Pick an issue or open a discussion describing the user problem and the smallest change that would solve it.
- Good first contributions: improve an agent setup guide, add a compatibility report for a specific client/version, tighten an API error message, add a regression test for message routing, or improve keyboard/mobile behavior in the dashboard.
- Before building a new wake adapter, show the host's documented interface and test whether it can actually resume the intended session. Keep MCP connection, message listener, notification, and model wake as separate claims.
- For a new endpoint or credential feature, explain its identity scope, who can call it, how revocation works, and what information the response reveals.

## Run the project

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest test_mailbox.py test_adapters.py test_collaboration.py test_control_access.py test_a2a.py test_workspaces.py test_dsh_wake.py test_dsh_notice.py test_zcode_wake.py
```

Use the local SQLite backend for tests. Never copy production messages, credentials, local listener manifests, screenshots with private data, or `.local/` files into an issue or pull request. Add a focused integration test when changing API behavior, identity routing, browser authentication, or a listener lifecycle.

## Pull requests

- Describe the user problem and resulting behavior, then list verification performed.
- Keep changes focused and preserve compatibility unless the release is intentionally breaking.
- Update `CHANGELOG.md` and `VERSION`. Patch releases increase the patch component; breaking/product milestones may bump the agreed minor or major component. Keep docs and landing-page claims aligned with implemented behavior.
- Never commit real tokens, passwords, customer data, generated production exports, or secrets. Tests must use disposable credentials and isolated local data.
- For adapters, state the exact host, version, supported events, and what remains unavailable.

## Host adapters

- Bind an exact native session ID to one inbox. Do not guess the latest chat when multiple sessions exist.
- Cite the host-supported API/CLI contract. State whether an integration offers MCP tools, notification delivery, turn-boundary injection, or actual idle wake.
- Preserve the host's sandbox and approval policy. Do not pass skip-permission flags.
- Read callback message JSON from stdin and avoid logging bodies, subjects or secrets.
- Leave server acknowledgement to verified processing. A queued prompt is not a completed request.
- Include a repeatable check for identity, routing, retry behavior and cleanup of owned fixtures.

Brand assets and public copy should follow [`brand/BRAND_GUIDE.md`](brand/BRAND_GUIDE.md); product claims must match shipped behavior. Social drafts remain drafts until the operator explicitly publishes them.

## Security reports

Do not publish an exploitable vulnerability in a public issue. Use GitHub's **Report a vulnerability** action on the repository's Security tab so maintainers can coordinate a fix privately. Include affected version, impact, and a minimal reproduction without real user data.

## Be kind and precise

Assume good intent. Review the behavior and evidence, not the person. Prefer small reproducible examples and say when a compatibility result is untested.
