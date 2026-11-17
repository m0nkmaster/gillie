# YellowPages service directory (organisation-specific)

Offer this only when `command -v yellowpages` succeeds.

- **Access:** `yellowpages --agent --fields <allowlist> ...` with an explicit, safe field allowlist.
- **Never use `--concise`:** service and team payloads can include credential fields.
- Read-only; no connector `deny` list is needed. Add a `check` only if a task depends on it.
