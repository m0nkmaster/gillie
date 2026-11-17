# Roosta - Roadmap

Ideas the maintainer has chosen to build, roughly in priority order. Shipped work moves to
`CHANGELOG.md` and its entry is removed here.

## Install, connect AI, start working

Harness setup is part of Roosta as of 0.4.0-0.8.0: named accounts, OS credential storage,
Claude/Pi dispatch, managed runtimes, Settings setup/tests and explicit legacy migration (see
`CHANGELOG.md`). What remains:

- **Confirm the Anthropic sign-in terms.** As checked on 2026-11-15, Anthropic's
  [SDK guidance](https://code.claude.com/docs/en/agent-sdk/overview) restricts third-party Claude.ai
  login, while its [Claude Code terms](https://code.claude.com/docs/en/legal-and-compliance) allow
  end users to sign into an unmodified binary, and its
  [subscription guidance](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan)
  still describes SDK subscription usage. Confirm how these apply to Roosta's packaged flow and
  to each organisation's policy; never collect subscription session tokens.
- **Windows runtime installation.** Managed runtime downloads cover macOS and glibc Linux on
  arm64/x64 only.

## Easy updates from Settings

One **Settings → Updates** experience for the browser UI and installed macOS app: show the running
version, check for a newer stable release, read what changed, then **Update and restart** with
progress and a clear result. Roosta detects the installation method; users should not have to
choose between GitHub and PyPI or learn package-manager commands.

- **Use the right distribution.** Packaged macOS installs update the whole app, including its
  bundled backend, from GitHub Releases. Python installs update the package from PyPI using the
  package manager/environment that owns that installation (`uv tool`, `pipx` or a virtualenv).
  Opening the web UI inside Electron does not make an externally managed server a bundled app.
  Never upgrade a separate Python install when the user is updating their packaged app.
- **Keep releases consistent.** One version and changelog across the Python package and desktop
  artifacts. Publish and verify the matching packages before announcing a release as available;
  exclude drafts, prereleases and withdrawn releases. Check compatibility and artifact availability
  for the detected installation, including Apple Silicon versus Intel.
- **Own the restart.** Updates from a browser affect the server serving that browser, not the
  browser itself. Identify that instance and allow installation only to its authorised operator.
  Wait for active chats/workers to finish, prevent new runs during replacement, and restart through
  the process/service that owns the runner. Reconnect the UI and confirm the expected version and
  health before reporting success. Preserve the previous working installation for recovery.
- **Make supported installs easy.** Use an established desktop updater and verify its signing and
  artifact requirements against our release pipeline. Python updates must target the detected
  environment with fixed arguments, never arbitrary commands supplied by a page. Development
  checkouts and externally managed deployments show accurate owner-specific instructions instead
  of overwriting source changes or pretending an update succeeded. Protect update actions from
  cross-origin requests and unauthorised remote callers.
- **Preserve the workspace.** Keep profiles, credentials, schedules, conversations and custom
  pages. Reuse core-skill sync so untouched skills update and edited skills remain available for
  merging; protect workspace changes needed by an upgrade so recovery remains possible.
- **Done means an actual upgrade works.** Exercise old-to-new updates for packaged macOS and
  supported Python installs, both foreground and service-managed, including offline checks,
  failed downloads, insufficient permissions, interrupted updates and restart failure. A release
  download link alone does not meet the easy-update goal.

## Token efficiency

- **Measure first.** Record input and cache tokens per run (from the stream's `result` usage) in the
  activity log and show them on the Workers page, giving a baseline per worker.
- **Choose MCP servers per worker.** Claude task runs load all ten user MCP servers in
  `~/.claude.json` and wait for each to connect. Load only what a worker needs:
  - derive it from `requires:` by giving each `connectors.yaml` entry an `mcp:` server, or add an
    explicit `mcp: [...]` to `tasks.yaml`, with checkboxes in the Workers settings drawer;
  - Claude: a per-task config with `--strict-mcp-config --mcp-config`; Cursor: a filtered `mcp.json`;
  - unset means all servers, as today, until every worker declares its own.
- **Further levers.** Cheaper `claude_model` for simple watchers; trimming loaded skills; checking how
  much sensor cache the conductor prompt reads.

## Assistant

- **Add a chat to the inbox.** An "Add to inbox" button on `chat:`, `doc:` and `person:` chats creates a
  card under the chat's own id, so the card and chat are one conversation. Title from the chat,
  `why` from its topic summary, optional bring-back time from the Not now presets. Stays until
  closed; the conductor does not re-check it. Open question: let the actor do this too ("keep this on
  my board").
- **Review and refine the `assistant` conductor skill.** Should it use `PRIORITIES.md`, and how?
  Today it reads it every pass and aims for roughly half the board to be proactive next steps on
  priorities, even when nobody asked. Check whether that earns its place against what the user acts
  on (`roosta usage`, closed versus ignored cards), whether priorities should rank reactive items
  instead of adding cards, and what happens when the file is empty or stale. Then tighten the skill.

## macOS app

- **Menu bar options.**
  - Top 3-5 inbox items, each opening `/assistant?item=<id>`.
  - "Ask Roosta…" opening a new chat, possibly with a global shortcut.
  - "⚠ N workers are sick", with a submenu naming each and why.
  - Running now (read-only; no Run now in the menu bar).
  - Pause notifications for an hour or until tomorrow.
  - Inbox count beside the icon (`tray.setTitle`).
  - The Office link.
  - Restart, View Logs, Test Notification, notification status moved under Diagnostics.

## Layout

- **Draggable column dividers on every page.** One shared `resize.js` (like `nav-count.js`): a
  `data-resize` handle sets a grid CSS variable; widths persist per page in `localStorage`;
  double-click resets; arrow keys for accessibility; minimum widths; off at mobile breakpoints.
  Assistant's document pane first, then Workspace, Pulse and Workers.

## Pulse

- **Make the constellation the page's focus.** Size it to the viewport (min 600px) rather than a fixed
  600px, put it first, and let the canvas use the full width until a star is selected, with the drawer
  sliding in and its width adjustable by the column dividers. Then polish the look once the specific
  complaints are named (labels, glow, rings, transitions).

## Workspace

- **Easy moves around a project.** A file strip above the preview: README first, then the project's
  other files by folder, the current one highlighted, `j`/`k` to step. Fixes arriving by search, where
  the middle column shows results rather than the project's files.

## Workers

- **See the skills that are not on a schedule.** A Skills tab beside Workers listing every skill
  in the workspace (and global roots), each with its description, whether a worker runs it, and a
  link to read it. An unscheduled skill gets **Put on a schedule**, which opens a `hire:` chat
  prefilled for that skill so the `tasks.yaml` entry is agreed before it is written; a scheduled one
  links to its worker row. Open question: separate core skills from the user's own.

## Possibilities

Not chosen yet; kept here so the option is visible.

- **Better chat controls.** Today a reply shows only a "thinking…" placeholder until it lands, and
  the only Cancel is on approval cards. Show what the chat is doing and let the user step in:
  - see its thinking and tool calls as they stream, collapsed by default;
  - stop a reply mid-run (kill the engine process; the background run in `chat.py` currently
    survives navigation by design);
  - reset a chat, or retry from an earlier message after correcting it;
  - halt or withdraw a message that was sent by mistake.
