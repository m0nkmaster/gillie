# Snowflake

- **Access:** the Snowflake CLI, `snow` (https://docs.snowflake.com/developer-guide/snowflake-cli). Connections live in `~/.snowflake/config.toml`.
- **Sign-in:** usually browser SSO (`authenticator = "externalbrowser"`) or key-pair auth configured by the user.

```yaml
- id: snowflake
  name: Snowflake
  check: snow connection test
  deny: [snow object create, snow object drop, snow stage copy]
```

- **Writes through SQL** cannot be caught by a command prefix, so give the connection a **read-only role**; that is the real guard.
- **Quirks:** keep queries bounded (`LIMIT`); name the warehouse and role in `TOOLS.md`. Record which schemas are canonical for which questions in a domain skill, not in `TOOLS.md`.
