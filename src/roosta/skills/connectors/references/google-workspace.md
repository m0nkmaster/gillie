# Google Workspace (Docs, Drive, Calendar, Gmail)

- **Access:** a Google Workspace MCP server or CLI. Many organisations provide one; otherwise use Google's current official option. OAuth client files live in the tool's own config directory.
- **Sign-in:** OAuth in the browser on first use.

```yaml
- id: google
  name: Google Workspace
  check: <cli> search recent --limit 1
  deny: [<cli> create, <cli> update, <cli> share, <cli> delete, <cli> send]
```

- **Record in TOOLS.md:** shared docs the user writes in regularly (a weekly update doc, team notes) with their URLs and due days.
