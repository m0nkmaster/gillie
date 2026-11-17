# Confluence

- **Access:** Atlassian's hosted MCP server (shared with Jira) or a Confluence CLI the organisation provides.
- **Sign-in:** OAuth on first use.

```yaml
- id: confluence
  name: Confluence
  check: <cli> user me
  deny: [<cli> page create, <cli> page update, <cli> page delete, <cli> comment]
```

- **Quirks:** on large Cloud sites full-text CQL (`text ~`) is swamped by migrated attachments; search by `title ~ "..."` first. Record the spaces that matter in `TOOLS.md`.
