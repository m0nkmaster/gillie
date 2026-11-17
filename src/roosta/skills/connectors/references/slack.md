# Slack

- **Access:** a Slack MCP server or CLI. Prefer one the workspace admin has approved; check Slack's current developer docs for the official option.
- **Sign-in:** a user or bot token issued by the workspace admin, stored in the MCP server's own config or `.env`, never in a skill or doc.

```yaml
- id: slack
  name: Slack
  check: <cli> auth test        # or an MCP-backed read via a small script
  deny: [<cli> chat post, <cli> chat update, <cli> chat delete, <cli> reactions add]
```

- **Record in TOOLS.md:** the channels that matter and which watcher owns each (board id prefix), how threads are permalinked, and paging/timestamp rules.
- **Quirks:** posts the assistant sends end with `[AI]`. Message timestamps (`ts`) double as ids; sort by them before reading.
