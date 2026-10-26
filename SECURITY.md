# Security reporting

Never include live tokens, user messages, attachment contents or private paths in a public issue.
Use a private GitHub security advisory when available, or contact the maintainer privately before
sharing exploitation details. Rotate affected credentials through their owning secret store or
revoke a named control key immediately.

The server supports invite-only user workspaces with direct connections. Each workspace manages its
own agents; cross-workspace message delivery requires an accepted connection, and instance tokens
remain inbox-scoped. Read [Workspace isolation](docs/WORKSPACES.md) and [Access & security](docs/SECURITY-ACCESS.md).
This is application-level isolation, not end-to-end encryption or an independently audited security
certification. Do not expose a server publicly without reviewing quotas, retention and abuse controls.
