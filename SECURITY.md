# Security reporting

Never include live tokens, user messages, attachment contents or private paths in a public issue.
Use a private GitHub security advisory when available, or contact the maintainer privately before
sharing exploitation details. Rotate affected credentials through their owning secret store or
revoke a named control key immediately.

The supported deployment model is one private server per person/team. Agent inbox isolation,
credential scopes and control-role limits are documented in [Access & security](docs/SECURITY-ACCESS.md).
The project does not currently provide public multi-tenant isolation or end-to-end encryption.
