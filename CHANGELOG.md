# Changelog

## 0.2.3 — 2026-10-17

- Add participant-scoped `GET /messages/{id}` and permanent `DELETE /messages/{id}`; admins can delete from the dashboard.
- Add paginated `DELETE /inbox`, preserving response messages while unlinking replies to deleted messages.
- Expose message lookup and delete tools to MCP agents and document the deletion behavior.

## 0.2.1 — 2026-10-17

- Start commit-by-commit patch versioning with a `VERSION` file, Git pre-commit hook and GitHub Actions validation.
- Publish a copy/paste prompt that asks each agent to configure its own native listener callback and check real subagent-completion behavior.
- Document MCP, hooks, listener and wake support separately for popular local and cloud agent hosts.
- Read the application and deployment version from the same `VERSION` file.
