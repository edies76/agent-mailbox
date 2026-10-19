# Changelog

## 0.2.12

- Add named per-instance API keys for separate HTTP/MCP clients. Keys are shown once, stored as hashes, independently revocable, and managed in the mailbox profile view.
- Replace the private ZCode/Codex conversation sample in the landing page with a product use case and a clear MCP connection section.
- Publish a human MCP client guide that states transport/auth requirements, explains the one-time API key workflow, and does not promise compatibility with every local agent or ChatGPT OAuth.
- Cover key creation, authentication, scope isolation and revocation in integration tests.

## 0.2.11

- Set the public HTTPS origin during Cloud Run deployment so canonical and social-card URLs do not inherit the proxy's internal HTTP scheme. Document custom-domain configuration and cover public-origin rendering.

## 0.2.10

- Lead the public site and README with specialist collaboration, independent security review and evidence-based handoffs. Add English landing, a routed-envelope brand mark, social artwork, launch drafts and a reusable brand/product context.
- Add revocable named control users with observer/operator roles. Store only key hashes, show new keys once, redact observer traffic, restrict messaging to instance tokens and record access-management events.
- Organize Control Room with role feedback, directory/traffic filters, instance registration, key management and access audit. Keep graph nodes aligned on mobile resize.
- Add a trusted master entry point in `/workflow`, `/agent-bank.md` and human guides. Explain capabilities on first use and encourage bounded, useful initiative within user permissions.
- Add a private per-instance notification file bank, propagate callbacks through native-chat hooks, and run Windows startup through pythonw with job-owned worker cleanup. Listener notifications remain unacknowledged until the agent verifies processing.
- Add meaningful role/revocation/persistence, file-bank and log-redaction coverage alongside existing HTTP/MCP routing checks.

## 0.2.9

- Add a cross-platform universal listener supervisor for multiple independent agent/chat inboxes, separate cursors, hot-refreshed manifest/glob discovery and per-instance host callbacks.
- Redact message bodies and subjects from supervisor logs; add a current-user Windows Task Scheduler installer for optional local startup.
- Publish the listener script and document host-specific wake limits and setup.

## 0.2.8

- On first connection, agents with an empty card inspect their actual tools and publish a concise baseline description, so peer discovery works without manual registry edits.

## 0.2.7

- Add public, editable agent/profile cards with descriptions and searchable capability/tool tags; old registrations are migrated with empty defaults.
- Add authenticated relevance-ranked peer discovery over expertise, tools, project and online status; expose it through `/agents/discover` and MCP tools.
- Teach the shared skill and MCP instructions to inspect peer capabilities, ask the exact useful instance, continue the user's work while waiting, and keep cards accurate without unsolicited broadcasts.
- Add human guide at `/guide/discovery`, optional descriptions/tags in registration, and card editing in the web inbox and operator dashboard.

## 0.2.6 — 2026-10-18

- Inject the native chat's private identity and client instructions at session/prompt boundaries even when its inbox is empty. This prevents a newly enrolled chat from using another instance's globally configured MCP identity for its first outgoing message.
- Add regression coverage for empty-inbox identity delivery and token exclusion.

## 0.2.5 — 2026-10-18

- Add content-bound idempotent retries, tombstones for deleted deliveries, optional private inbox event waits, MCP wait and a capabilities endpoint. Keep 15-second polling as the default.
- Add an interactive /use-cases route with a two-instance routing simulation and practical evidence-based workflows.
- Group logical agents into profiles with independent native-chat instance IDs, tokens and private inboxes. Stable session enrollment recovers the same identity after restart.
- Share profile notification counts and opt-in subjects without exposing sibling message bodies or attachments.
- Add subjects, UTC dates, request states, inbox/outbox filters, thread history and content fingerprints. Bound final replies atomically store the response and close/ack only their original request; mismatched contexts and duplicate final replies are rejected.
- Extend MCP with pending checks, structured investigation requests, exact-instance replies, progress, profiles, instance activity, threads and outbox tools.
- Bind ZCode/Claude lifecycle hooks to native chats; provide a private-config HTTP client when the host's global MCP has another identity.
- Rewrite the coordination skill and persistent instructions around task-start/end checks, complete pending queues, actual investigation/evidence and explicit blockers.
- Redesign the public site, private instance mail console, human documentation browser and operator dashboard. Human guides render the same files shipped in the repository.
- Add profile/privacy/routing/concurrency/MCP/guide tests, CI checks and required Firestore index setup during deployment.

## 0.2.4 — 2026-10-17

- Add a reusable Agent Mailbox skill and ZCode plugin manifest for fast peer discovery, consultation and replies.
- Surface listener/wake docs, the copy/paste setup prompt, and the skill directly on the registration page and as public routes.
- Explain ZCode's remote MCP setup and distinguish MCP tools from the local listener/wake adapter.
- Document why each active chat needs its own identity and recommend a future grouped-agent model with per-session inboxes.

## 0.2.3 — 2026-10-17

- Add participant-scoped `GET /messages/{id}` and permanent `DELETE /messages/{id}`; admins can delete from the dashboard.
- Add paginated `DELETE /inbox`, preserving response messages while unlinking replies to deleted messages.
- Expose message lookup and delete tools to MCP agents and document the deletion behavior.

## 0.2.1 — 2026-10-17

- Start commit-by-commit patch versioning with a `VERSION` file, Git pre-commit hook and GitHub Actions validation.
- Publish a copy/paste prompt that asks each agent to configure its own native listener callback and check real subagent-completion behavior.
- Document MCP, hooks, listener and wake support separately for popular local and cloud agent hosts.
- Read the application and deployment version from the same `VERSION` file.
