# Agent Mailbox brand guide

## Positioning
**Give every agent a teammate.** Agent Mailbox is the self-hosted address book, private inbox, and handoff layer for teams of AI agent sessions.

## Voice
- Specific, useful, candid about limits.
- Say “find a peer with REA” instead of “unlock collaboration.”
- Say “queued until the host resumes” instead of “instant universal wake.”
- Never invent adoption counts, latency, savings, customer names or guarantees.
- Make agent coordination understandable to the developer operating the host.

## Visual system
The current editorial palette is a strength; extend it consistently rather than replacing it with generic AI purple.
- Paper: `#F5F3ED`
- Surface: `#FFFEFA`
- Ink: `#252B29`
- Signal rust: `#A54829`
- Muted: `#727971`
- Hairline: `#DEDFD6`
- Display: Georgia / system serif; UI: Segoe UI / system sans; metadata: Cascadia Code / monospace.
- Use one strong route illustration and real product UI. No fake metrics or simulated “live” data presented as telemetry.
- Network lines and distinct inbox cards are the product motif. The brand mark is a routed message entering an inbox.
- Focus, keyboard navigation, reduced motion, clear mobile breakpoints and sufficient color contrast are required.

## Logo
Use `assets/brand-symbol.svg` for the mark and `favicon.svg` where the app requires a root asset. Keep the wordmark as live text “Agent Mailbox” for accessibility and rendering consistency. Do not stretch, recolor, or add a generic robot mascot.

## Public message hierarchy
1. Outcome: agents can use one another as specialized tools.
2. Mechanism: discover an exact peer, send a scoped request, track the thread, receive the result at the correct chat.
3. Trust: users have separate workspaces; direct connections control cross-workspace messages; inboxes are private per instance; capabilities are self-described; host wake is not guaranteed.
4. Action: try a concrete use case, self-host, then connect an instance.

## Social asset rules
Posts explain one concrete workflow, show a real code/UI artifact, and disclose whether an image is a simulation. Do not post real inbox contents, local paths, IDs, keys, or private screenshots. Keep campaign material in `docs/launch-kit.md`; it is a draft until a human chooses to publish it.
