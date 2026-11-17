# Notion

- **Access:** Notion's hosted MCP server, `https://mcp.notion.com/mcp` (Streamable HTTP, OAuth). There is no official CLI. `claude mcp add --transport http notion https://mcp.notion.com/mcp`, then authenticate once with `/mcp`.
- **Check:** none from the shell; leave `check` out and use the connector entry for `deny` only. A headless run may report `needsAuth` even when interactive sessions work: say so and ask for the content rather than reporting a page missing.

```yaml
- id: notion
  name: Notion
```

- **Record in TOOLS.md:** the ids of databases the user relies on (a task list, a reading list) and their columns. If Notion holds the user's tasks, say so: the workspace never mirrors a task list.
