---
name: connectors
description: Connect a tool (Teams, Slack, Jira, Confluence, Google Workspace, Outlook, Notion, GitHub, Snowflake and others) to this Roosta workspace - install its CLI or MCP server with the user's approval, add a sign-in check and write denials to .roosta/connectors.yaml, and record how to use it in TOOLS.md. Use when the user asks to connect, add, set up or fix a tool, when a task needs a tool that is missing, or when a connector shows as signed out.
---

# Connectors

A connector is everything Roosta needs to use one tool safely:

1. **Access** - a CLI or MCP server the agent can call.
2. **A sign-in check** - a cheap read-only command in `.roosta/connectors.yaml`. A task that lists the connector in `requires:` runs it just before starting, and waits as `blocked` (showing the `login` command) instead of spending a model run that would fail.
3. **Write denials** - the tool's write commands, listed under `deny`, so no unattended run can use them. Writes only happen through the approval card and the `sender` skill.
4. **Know-how** - a `TOOLS.md` entry: what the tool is for here, how to reach it, the check, and its quirks.

The catalogue in `references/` has a file per known tool. Use it as the starting point, verify against the tool's current documentation, and never invent a command.

## Connecting a tool

1. **Find the catalogue entry** in `references/<tool>.md`. Entries marked *organisation-specific* apply only when their CLI is already on the machine (`command -v <cli>`); never offer them otherwise. No entry? Research the tool's official CLI or MCP server and follow the same shape.
2. **Check what is already there**: `command -v <cli>`, `.mcp.json`, `claude mcp list`. Prefer what the user already has.
3. **Install only with approval.** Show the exact install command and where it comes from, and wait for a yes. One install per approval.
4. **Sign in.** Interactive sign-in is the user's to do: give them the command and wait. Never handle passwords or tokens yourself, and never print them.
5. **Add the connector** to `.roosta/connectors.yaml`:

   ```yaml
   connectors:
     - id: github                 # lower-case; tasks use it in `requires: [github]`
       name: GitHub
       check: gh auth status      # read-only; exit 0 means signed in
       login: gh auth login       # what the user runs when it fails
       deny:                      # command prefixes unattended runs may never use
         - gh pr create
         - gh issue create
   ```

   With JSON output, `expect` and `reject` read fields instead of the exit code (`expect: {authenticated: true}`, dotted paths allowed). Changes apply without a restart.
6. **Run the check** yourself and confirm it passes.
7. **Write the `TOOLS.md` entry**: one paragraph with purpose, route, check and known quirks.
8. **Wire the tasks** that need it with `requires: [<id>]` in `.roosta/tasks.yaml`, and suggest a watcher skill if the user wants the tool on their board.

## Read versus write

MCP tools are classified by name: a hook allows read-shaped tools and blocks names containing `create`, `update`, `delete`, `move`, `duplicate`, `send`, `post`, `comment`, `trigger` or `deploy`. A CLI has no such convention, so its writes must be listed under `deny`. When unsure whether a command writes, deny it.

## Fixing a signed-out connector

A task waiting on a sign-in shows `blocked` on the Workers page with the connector and its `login` command. Run the connector's `check` to see the error, have the user sign in, then Run now on the task. A check that errors (the command itself broke) never blocks tasks: fix the command rather than the sign-in.

## Related

| Link | Relationship |
|------|--------------|
| [[setup]] | Connects the user's tools during first-run setup |
| [[sender]] | The only path for approved writes |
