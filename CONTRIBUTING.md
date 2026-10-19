# Contributing

Start with a concrete workflow: which agent has a tool, which chat needs evidence, and how the result
returns to the original requester. Open an issue or PR with the expected behavior and relevant host
version. Keep credentials, private inbox messages and local paths out of reports.

## Development

Use Python 3.14 and `requirements.txt`. Run the relevant HTTP/adapter tests and `test_control_access`
for changes involving permissions or the local bank. The Git hook increments VERSION by one patch
per commit; do not amend a published release. Human guides are rendered from their repository source.

## Host adapters

- Bind an exact native session ID to one inbox. Do not guess the latest chat when multiple sessions exist.
- Document whether the integration offers MCP tools, notification delivery, turn-boundary injection,
  or actual idle wake. Show the host-supported API/CLI contract.
- Preserve the host's sandbox and approval policy. Do not pass skip-permission flags.
- Read callback message JSON from stdin and avoid logging bodies, subjects or secrets.
- Leave server acknowledgement to verified processing. A queued prompt is not a completed request.
- Include a repeatable check for identity, routing, retry behavior and cleanup of owned fixtures.

Brand assets and public copy should follow `brand/BRAND_GUIDE.md`; product claims must match shipped
behavior. Social drafts remain drafts until the operator explicitly publishes them.
