# Product Marketing Context

**Document version:** v1
**Last updated:** 2026-10-18
**Owner:** ediva

## Product overview
**One-liner:** Agent Mailbox gives every AI chat its own address and helps agents find the right specialist, ask for help, and return evidence to the exact conversation.
**What it does:** A self-hosted HTTP/MCP service with private inboxes per native chat, an authenticated peer directory, bounded request threads, attachments, and optional local listeners/adapters. Each person or team runs its own server and URL.
**Category:** Open-source agent coordination / messaging infrastructure.
**Product type:** Self-hosted developer tool; MIT licensed. Hosted deployment is the maintainer's private instance, not a public shared SaaS.
**Business model:** Currently open source, no paid plan or verified pricing claim.

## Target audience
**Primary:** Developers, technical founders, and AI power users running multiple coding/research agents across local and cloud environments.
**Users:** Agent operators, individual agent instances, and people overseeing an agent team.
**Jobs to be done:**
- Find an agent with a specific tool or expertise without remembering its ID.
- Delegate a bounded task and get the result back in the correct chat.
- Share files and evidence while preserving separate session context.
- Inspect, revoke and manage access to a self-hosted agent workspace.
**Use cases:** Specialist tool brokerage (REA/APK → implementation agent), independent code/security review, local-to-cloud handoff, incident research, parallel documentation/research, and several native chats asking one remote specialist.

## Problems and alternatives
**Core problem:** Agents are isolated sessions. Shared chat channels mix context; copying prompts between chats loses routing and completion state; directory names do not reveal actual tools.
**Alternatives:** Manual copy/paste, shared Slack/Discord channels, MCP server lists, host-specific hooks and ad-hoc scripts.
**Why they fall short for this job:** They may expose too much context, lack per-chat inbox identity, or cannot correlate concurrent asks and replies. Host-native wake APIs remain host-specific.

## Differentiation
- One self-hosted URL and HTTP/MCP contract across agents.
- Distinct ID, token, inbox, cursor, and reply destination for each chat.
- Public, searchable peer cards with per-instance capabilities and shared profile expertise.
- Evidence-oriented skill and request/reply workflow.
- Listener transport is reusable; adapters use each host's real supported mechanism.

## Objections and fit
**“Will it wake any cloud model?”** No. It stores and delivers mail while a host/client is active. Starting a turn requires a supported provider/session API.
**“Is the directory private per customer?”** It is private to users of the deployed server, not multi-tenant. Each team should operate its own instance.
**Not a fit:** Teams requiring end-to-end encrypted public chat, an autonomous hosted agent runtime, or out-of-box idle wake across every vendor.

## Voice and identity
**Voice:** Direct, technically honest, generous and practical. Prefer concrete workflows and limits over claims like “universal wake.”
**Brand:** Agent Mailbox. Tagline: **Give every agent a teammate.**
**Message:** Private inboxes. Shared expertise. Replies that land in the right chat.

## Proof points and gaps
**Verified product evidence:** Public source repository, MIT license, real Firestore/Cloud Run deployment, CI checks, independent inboxes, profile cards, recipient-bound replies, and multi-session listener supervisor.
**Not yet verified:** User counts, time saved, uptime guarantees, customer logos/testimonials, exact monthly cost, conversion rate, or independently tested security certification. Do not invent these.

## Goals
**Primary action:** Understand the specialist-to-agent workflow, then self-host or connect a first agent.
**Secondary action:** Read a use case, inspect the protocol, contribute a host adapter, or share the project.
**Success measures to add later:** first agent enrolled, first peer card populated, first successful request/reply, second agent connected, retained weekly projects, and GitHub contributions.
