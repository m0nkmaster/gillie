# Jira

- **Access:** Atlassian's hosted MCP server (Jira and Confluence, OAuth sign-in) or a Jira CLI the organisation provides. Check the vendor's current docs for the server URL before adding it to `.mcp.json`.
- **Sign-in:** OAuth through the MCP client on first use, or the CLI's own login.

```yaml
- id: jira
  name: Jira
  check: <cli> user me          # any cheap authenticated read
  deny: [<cli> issue create, <cli> issue update, <cli> issue transition, <cli> comment]
```

- **Record in TOOLS.md:** the site URL, the projects that matter, labels or boards that identify the user's team, and any field every new ticket needs (an owning team, a component). The `sender` skill refuses a create that lacks a required field.
- **Quirks:** always include the ticket link when mentioning a ticket. Never bulk-close tickets. Cloud descriptions are Markdown through most CLIs and ADF through the REST API; wiki markup renders literally.
