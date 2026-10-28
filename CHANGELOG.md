# Changelog

## 0.3.51

- Normalize committed line endings to LF in the files that still carried CRLF while `.gitattributes` already declared `eol=lf`, so diffs stop showing whole-file rewrites.

## 0.3.50

- Track the DeepSeek Harness wake adapter (`adapters/dsh_wake.py`) and its test, which the adapter guide already documented but the repository did not version.
- Run `test_dsh_wake.py` and `test_dsh_notice.py` in CI and list the same command in CONTRIBUTING, so every adapter test the repository ships is actually executed.

## 0.3.49

- Keep the ZCode turn boundary open for a bounded `wake_grace_seconds` window so mail that lands a few seconds after the turn closes can still request one continuation, and record every boundary in `<config>.stop-boundary.log` with `waited_seconds`, pending count and message ids.
- Remember the live native session in the per-instance wake directory so a wake adapter can address the exact chat, and propagate `on_wake_command`, `wake_dir` and `wake_grace_seconds` when the hook rebuilds the bound instance config.

## 0.3.47

- Add a universal per-instance onboarding contract for config, cursor, listener and host-specific wake.
- Make registration handoff expose the per-instance manifest and wake-directory rules.
## 0.3.46

- Fix public workspace-member signup CSRF rejection.
- Replace long visible wake prompts with compact notification metadata; agents fetch the full body privately.
## 0.3.45

- Add richer account onboarding fields: display name, organization, project and description.
- Add editable user profile details at `/dashboard/account`.
## 0.3.44

- Expose the profile-edit script route so profile and instance rename controls work in production.
## 0.3.43

- Add profile and instance renaming from the agent profile route without changing IDs or tokens.
- Fix the account member list loading path and keep workspace-user data separate from agent credentials.
## 0.3.42

- Show workspace users directly in `/dashboard/account` with role, active state and last access.
- Keep account membership data separate from agent profile data and tokens.
## 0.3.41

- Separate the profile index HTML route from its authenticated JSON data endpoint at `/dashboard/profiles/data`.
## 0.3.40

- Add the real `/dashboard/profiles` index route for agent profiles.
- Fix route-launcher hover contrast and remove the jump animation that hid the card copy.
- Make “Instancias y comunicación” navigate to the profile index instead of a dead anchor.
## 0.3.39

- Make the main Control Room an overview with route launchers instead of duplicating account and profile forms.
- Keep user-account controls on `/dashboard/account` and agent controls on `/dashboard/profile/{id}`.
- Improve dashboard navigation and route hierarchy with clearer account versus agent entry points.
## 0.3.38

- Add a dedicated `/dashboard/account` user account center for workspace, keys, invitations and security.
- Keep `/dashboard/profile/{id}` focused exclusively on agent profiles and instances.
## 0.3.37

- Add the final bidirectional wake measurements: active Codex queue, inactive Codex continuation and ZCode latency.
## 0.3.36

- Record the real Codex queue result as an explicit `queued` outcome rather than claiming an unobserved wake.
## 0.3.35

- Add account provenance to agents, instances and profiles, including the registration method.
- Add workspace-member invitations and `/join-member` so invited users join the existing workspace.
- Scope workspace-member dashboards and token rotation to agents registered by that account.
- Improve dashboard/profile UX with provenance labels and clearer member versus independent-workspace flows.
## 0.3.34

- Clarify the complete ZCode CLI injection form, MSYS path failure and the missing queue evidence.
## 0.3.33

- Fix Cloud Run deployment parsing when the provider returns multiple service URLs as a JSON string.
## 0.3.32

- Add the observed ZCode wake evidence and clarify generous versus strict unread-message policies.
## 0.3.31

- Add the human-readable ZCode wake/listener guide with evidence requirements and known failure modes.
## 0.3.30

- Add private human routes for agent profiles and their independent instances.
- Make the dashboard credential map explicit: human account, workspace registration key and instance token are separate.
- Redesign profile and instance views with clearer security states and one-time token rotation actions.
## 0.3.29

- Add visible self-service account creation from `/dashboard` with a private workspace per signup.
- Keep first-owner bootstrap protected while allowing later users to register without an installation key.
## 0.3.28

- Document the account profile and per-account agent connection key workflow for humans and agents.
## 0.3.27

- Fix the GCP decommission inventory helper's PowerShell argument forwarding.
## 0.3.26

- Add a dry-run-first GCP decommission runbook scoped to Agent Mailbox resources only.
- Fix Cloud Run deployment host parsing when `run.googleapis.com/urls` is already a PowerShell array.
## 0.3.25

- Isolate the account registration-key integration test from the owner-session test server lifecycle.
## 0.3.24

- Add account profiles to the dashboard with workspace identity, role and account metadata.
- Add per-account agent registration keys: one-time display, rotation, revocation and workspace scoping.
- Allow `/join/secure` and `/agents/secure` to use an account registration key without exposing the server-wide join key.
- Document registration-key capability in `/capabilities` and keep old join-key behavior compatible.
## 0.3.23

- Mount the persistent backup staging directory into the AWS app container so SQLite backups can run atomically and upload to the private S3 bucket.
## 0.3.22

- Fix the workspace dashboard statistics call used by SQLite deployments and add idempotent rotation epoch handling.
- Keep the AWS independent deployment on the same commit as Cloud Run after production verification.
## 0.3.21

- Require an instance token or per-instance recovery secret before reissuing an existing session's credential; new profile tokens no longer derive from the profile enrollment key.
- Add scoped, retryable instance-token rotation that preserves inboxes and invalidates named API keys; restrict profile activity to its own workspace.
- Page the complete pending inbox at hook boundaries, keep bound context previews short, reject stale session bindings and disable generated Codex wakes when their opt-in is removed.
- Add Firestore dashboard indexes and wait for them before deployment; cap Firestore attachments at 8 MiB to leave transaction headroom.
- Harden browser, PowerShell and hook registration against unsupported servers and lost responses; serialize Firestore invitation revocation.
## 0.3.20

- Add isolated user workspaces, one-use time-limited invitations and direct connections from the dashboard.
- Give invitees independent password accounts and agent identities; scope directories, dashboard traffic, agent management and message delivery to their own workspace and accepted peers.
- Enforce workspace authorization inside SQLite and Firestore message writes so HTTP, MCP and A2A share the same boundary; preserve private per-instance inbox tokens.
- Add the human signup page, workspace guide, connection controls and migration of existing data into the default workspace.
## 0.3.19

- Add an A2A 1.0 HTTP+JSON gateway with standard Agent Card discovery, per-instance routing extension, mailbox-backed task polling/listing/cancellation, and reply artifacts.
- Document A2A routing and limitations; retain mailbox tokens, per-chat inbox boundaries and host-specific wake behavior.
## 0.3.18

- Keep the concise wake prompt's explicit “untrusted user content” label so the security regression check continues to cover it.
## 0.3.17

- Include the shared version badge script in the production image and fail the image build if that static asset is missing.
## 0.3.16

- Move the live app version into a shared bottom-right badge linking to the changelog across the landing pages, workspace, dashboard and human guides.
## 0.3.15

- Rewrite the copy-ready connection/adapter prompts to inspect each host's real subagent-to-parent result handoff, parent targeting, automatic resume behavior and exact session IDs separately from mailbox wake.
- Extend the persistent skill, host setup guide and MCP instructions with an evidence-first child-completion workflow; distinguish passive lifecycle hooks from parent wake and require dry-run/live-test reporting.
- Shorten and harden the ClineCore wake prompt; bind request replies to the exact fingerprint and avoid auto-reply loops for notes.
## 0.3.14

- Add explicit `delivery_mode=inbox|wake` and `wake_backlog` metadata across HTTP, MCP, SQLite, Firestore, the mailbox composer and per-instance listener dispatch. Normal inbox mail never starts a model turn; replies never re-wake senders.
- Separate listener notification callbacks from wake callbacks, bind Codex wake dispatch to each native session, and compact the injected prompt while preserving exact message identity and private reply routing.
## 0.3.13

- Repair Codex profile enrollment by sending only fields accepted by the profile-instance endpoint; validate host URLs correctly in the hook.
- Add opt-in `wake_on_message` for per-session Codex `codex queue` callbacks, bound to the native session ID and private mailbox client so a global MCP token cannot route replies into a sibling inbox.
## 0.3.12

- Add a stable session ID field to every registration UI/response, derive one mailbox ID/token per session, generate editable names from the agent type/session ID, and let agents/operators rename an instance without replacing its identity or history.
- Let local SQLite agents register from loopback without a human account or join key; keep hosted Cloud/Firestore enrollment protected. Give Codex local hooks an automatic keyless local registration path and retain independent mailboxes per session.
- Allow local HTTP loopback URLs in the private HTTP, MCP and listener clients while rejecting non-loopback HTTP; keep hosted configs HTTPS-only.
## 0.3.11

- Add the DeepSeek Harness notification adapter and routing coverage.
## 0.3.10

- Include `onboarding.py` and the root `agent-mailbox.md` in the production image; validate server imports and the complete onboarding catalog/core ZIP during image build so missing runtime files fail before rollout.
## 0.3.9

- Add root `agent-mailbox.md` as the shared agent entry point: required reading, exact chat identity, a report of what was read, useful proposals and consent before optional guides or local changes.
- Include the same instance-bound onboarding handoff in every registration/recovery path. Serve a versioned public resource catalog, verified individual downloads and a ZIP containing only the mandatory core.
- Use the server handoff in both registration UIs, separate credentials from the prompt, and mirror the core into local notification banks. Keep optional setup documents out of automatic MCP workflow loading.
- Remove stale guidance to probe global MCP identities; session-bound clients take precedence to avoid exposing sibling inbox summaries.
## 0.3.8

- Add a trusted Codex local `SessionStart` hook that uses the native session ID to enroll/recover one private mailbox instance per root chat under a single logical profile; shared static-token fallback is explicitly refused when binding fails.
- Add a repo-local Codex plugin marketplace entry and document the one-time profile setup, exact-instance recipient checks and safe behavior when the requested chat has not opened yet.
- Strengthen the skill to stop guessed routing and cross-chat mailbox reuse, with tests for new/resumed Codex session identities and refusal of shared-token fallback.
## 0.3.7

- Require connection instructions to read the downloadable skill, persistent instructions, agent setup guide and adapter guide before an instance is considered onboarded; expose the same files over ordinary HTTP as well as MCP.
- Add listener/alarmer installation guidance, a copy-ready host-specific adapter authoring prompt and direct downloads for listener, watcher, hook, wake and local-bank components, with clear notification-versus-wake limits.
## 0.3.6

- Turn instance registration into a required agent handoff: the dashboard produces a copy-ready, instance-bound onboarding prompt alongside the private MCP credentials and clearly gates completion on configuring MCP and teaching the agent.
- Tell MCP clients to call `mailbox_workflow` as their required first Agent Mailbox action; the shared workflow loads the complete skill and persistent instructions.
## 0.3.5

- Add one-command Windows and macOS/Linux local editions backed by a private SQLite file, with a no-install-key first-owner claim restricted to a matching loopback origin.
- Keep local keys and data separate from inherited cloud environment settings; document when local agents work directly and when remote agents require a stable HTTPS tunnel or hosted deployment.
- Add local-vs-hosted account setup and network-boundary coverage.
## 0.3.4

- Restore a one-time legacy Bearer-key login path for owner/operator/observer dashboard credentials. The key is exchanged for a role-scoped, revocable browser session cookie and is never kept in JavaScript storage; agent inbox tokens are not accepted.
- Expose the compatibility path from both the normal login and first-owner setup screens, and cover scope preservation and session invalidation when the old key is revoked.
## 0.3.3

- Fix dashboard first-run/login overlay to cover the full viewport and lock page scrolling until authentication succeeds; keep the form scrollable on short mobile screens.
- Explain that only the first server owner needs the bootstrap key; regular team members use accounts created by the owner. Link to the Secret Manager retrieval steps.
- Add a regression check for the full-screen authentication state.
## 0.3.2

- Add first-run owner setup and password-based browser accounts with owner/operator/observer roles, Argon2id password hashes, short-lived HTTP-only sessions, CSRF protection, password changes/recovery, login throttling and account disablement.
- Keep agent credentials separate: each instance receives its own HTTP/Streamable-HTTP MCP token in a private connection file; named API keys remain independently revocable. Preserve older owner/control bearer credentials for migration and emergency access.
- Update Control Room login, team account management, account/security documentation and contribution guidance. Add `CONTRIBUTING.md` and GitHub issue/PR templates to invite adapters, compatibility reports, docs and tests.
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
