# Outlook and Microsoft 365 mail and calendar

- **Access:** a Microsoft 365 connector or MCP server. Organisations often approve read tools and require admin approval for writes.
- **Sign-in:** Microsoft SSO on first use.

```yaml
- id: outlook
  name: Outlook
  check: <cli> mail search --limit 1
  deny: [<cli> mail send, <cli> mail reply, <cli> event create, <cli> event update]
```

- **Quirks:** where write tools are refused by policy, the `sender` cannot apply an approved change; give the user the exact text to apply themselves.
