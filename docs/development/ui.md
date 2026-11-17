# Shared UI, workspace and Pulse

[Agent guide](../../AGENTS.md). Read before changing any page. Paths beginning `static/` are under `src/roosta/`.
See [Assistant contracts](assistant.md) and [Office rendering](office.md) for page-specific details.

## Shared shell and components

- **`static/nav-count.js`** - The one shared header. Every page, shipped or custom, carries an empty
  `<header class="workspace-header navbar" aria-label="Workspace"></header>` and loads this script, which
  renders the brand, the nav (shipped pages, then custom pages), the Assistant count and, top right, the
  version and a Settings cog. Settings is never a nav link; the macOS app menu also offers it (Cmd+,).
  Add or rename a nav link in the script's `NAV` list only, never in a page

- **`static/changelog.html`** - `/changelog`, renders `CHANGELOG.md` via `/api/changelog`, which
  also returns the version (the top `## x.y.z` heading). `nav-count.js` adds that version, linked
  here, to the top right of every page's header

- **`static/settings.html` / `ai-settings.js` / `theme.js`** - `/settings` leads with the default AI account and names the software it runs through. Existing Claude Code connections explicitly explain that Roosta reuses the login on this computer; a detected but untested login prompts a connection check. Model and reasoning controls live in a disclosure, with a model dropdown grouped by tested account and an Effort dropdown when the selected model supports it. Supported effort levels load from the runtime and persist with the model choice. Choosing a model saves the default account and provider-scoped model together. Account setup lives under the collapsed Change AI or manage accounts disclosure (opened automatically when no tested account exists). Account management and usage sit in per-account disclosures (usage lists at most the 10 most recently active chats per account; `chat_count` carries the total); missing software has an inline install action on the account; AI software also offers installation, Open in Terminal on macOS, and Update to latest under a closed Updates disclosure, preserving other installations. Terminal sessions use the CLI’s own login and settings, as explained beside the controls. Service names use Claude and ChatGPT / OpenAI; account names are optional. Add/edit forms have Cancel, clear credentials on exit and stay open across job polling; expanded account details also survive polling. Provider/billing guidance, official sign-in and the disclosed tool-use test remain part of setup. Settings shows a Continue workspace setup notice above Your AI only while setup is unhandled and a concrete output is missing (name, priorities or starter schedule), naming what is missing; chat history alone never shows it. A Workspace and runner card (`workspace-settings.js`) follows the account setup: active workspace and a Change workspace disclosure (select, create or adopt, with a Git option), the address `roosta start` will use (hidden for the desktop app; a non-loopback host needs an explicit confirmation), background-service status and install, and Sync core skills. Nothing there switches or restarts the running runner; the Assistant’s preceding Connect your AI card links here without starting an AI turn. Only appearance is a per-browser preference in `localStorage`: Light mode (`roosta-theme` = `light`, absent means dark). Every page loads `theme.js` undeferred
  right after `shell.css`, so `data-bs-theme` is set before first paint; the `storage` event keeps open
  pages in step. The light palette is one `[data-bs-theme="light"]` block in `shell.css` that redefines
  the `--console-*` and `--signal-*` tokens (signals darken to stay legible on white). Pages use those
  tokens (`--console-rail`, `--console-selected`, `--console-tint`, ...) rather than hardcoded dark hexes,
  or light mode leaves dark patches. The constellation canvas carries its own `data-bs-theme="dark"`
  because it draws light text; the Office canvas and Ceefax TV are art and stay as they are

- **Folder browsing** (`GET /api/settings/folders`) keeps listing readable folders when a child's
  `.roosta` marker cannot be inspected; that child has no workspace badge. An unreadable selected
  folder still returns an error. The endpoint remains restricted to local Settings requests.

- **`static/shell.css`** - Shared Tabler theme tokens, local Inter font, top navigation, page headers and focus
  styles; keep page-to-page shell geometry and palette here so switching views feels continuous

- **`static/vendor/tabler/`** - Pinned Tabler 1.7.3 core CSS and MIT licence; no build step or plugin JS

- **Brand artwork.** The shared header and browser tabs use `static/favicon.png`, copied from
  `assets/roosta-pixel.png` by `desktop/scripts/create-icons.js`.

- **Visual identity is a professional Tabler workspace.** Use the locally vendored Tabler core CSS,
  Inter and the shared slate theme in `shell.css`. A minimal horizontal bar carries navigation
  at the top; each page has a clear header. Assistant separates the inbox, conversation and optional
  document pane without an enclosing card or nested navigation frame. Keep full-width content with a fixed speaker
  column. Action types keep stable, restrained colours in tags and selection rules; statuses remain
  legible text with a dot. Avoid decorative metric rainbows, default blue branding, brown tones,
  narrow centred chats, pill-heavy controls and bespoke display typography.

- **Tabler owns components.** Use `.btn`, `.btn-primary`, `.btn-danger`, `.btn-outline-danger`,
  `.btn-ghost-secondary` and `.btn-sm`; inputs/selects use `.form-control`/`.form-select`, and panels
  use `.card`/`.card-header`. Do not rebuild a parallel control family in page CSS. Primary actions
  are violet, external approvals use danger, dismissive actions use ghost. Keep tags flat and
  smaller than buttons; status labels must never look clickable. `.task-status` is the runner's
  status hook, distinct from Tabler's category `.badge`.

- **Load locally, in order.** Every page loads `vendor/tabler/tabler.min.css` before `shell.css`.
  Tabler 1.7.3 was extracted from its integrity-verified npm archive; provenance and update guidance
  are in `static/vendor/tabler/README.md`. Keep the licence beside it. Existing JavaScript handles
  interactions, so neither Tabler nor Bootstrap JavaScript is needed. Inter is in `static/fonts/`.
  Page-local CSS describes workflow geometry, prose and state; shared branding and component tokens
  belong in `shell.css`. Navigation labels stay visible in smaller windows; only the brand text hides.
  Use native card bodies/headers, list groups, badges, alerts and dropdown items; do not repaint
  their borders, fills or padding locally. Grid rows may set their signal edge with a scoped selector
  (`.task-list .task-row`, `.item-list .item-row`) so Tabler's flush-list rule retains it.

- **No special-effect button variants.** Say what an action does; AI ornament adds no information
  when nearly everything is AI. Keep a clear distinction between saturated action buttons, tinted
  category tags and status text. External approvals must remain separate from ordinary chat sends.

- **CSS freshness.** `/static/*.css` is served `Cache-Control: no-cache`. Reloading picks up HTML/CSS
  changes without a runner restart or Electron rebuild. Electron's logs and recovery windows retain
  their separate styles.

## Custom pages

- **Custom pages** - static HTML/JS in the workspace's `.roosta/pages/<name>/`, served at
  `/pages/<name>/` and added to every page's nav by `nav-count.js` (from `/api/pages`, cached in
  `localStorage` so the link paints at once; folders that override a shipped page are not listed). They read
  only `/api/vault/files?glob=` (frontmatter, no bodies), `/api/vault-document`, `/api/tasks`,
  `/api/board`, `/api/profile` and `/api/pages/<name>/data/<file>.json`, which serves JSON skills
  write to `.roosta/state/pages/<name>/`. A page folder named like a shipped page replaces it
  (`page_file`). There are no Python plug-ins: a missing capability becomes a generic API here.

## Workspace and documents

- **`static/document.html`** - Read-only Markdown renderer for validated vault documents linked
  from Assistant items

- **`static/workspace.html` / `workspace.js`** - Fluid Tabler document navigator; in-memory literal
  search over `/api/workspace`, project/topic/people/decision collections, highlighted previews and
  up to eight selected files; collection names, descriptions and membership come from vault metadata

- **`static/vault-markdown.js`** - Shared sanitised Markdown renderer for document and Workspace previews

- **`static/markdown-copy.js`** - Copy-as-Markdown for the document page, Workspace preview and
  the whole Assistant board (items, chats, document pane; text fields keep native copy): `watch(selector)` makes highlighting (or Cmd+C) copy the selection via
  Turndown (CDN, GFM plugin); `copyMarkdown(text)` backs each Copy button. Copies use
  `execCommand('copy')` because the desktop app denies every permission, async clipboard included

- **Workspace search never runs AI.** `/api/workspace` returns visible Markdown contents with
  `no-store`; literal matching and highlighting stay in browser memory. Selected paths persist per
  chat, and `api_board_chat` reads full contents afresh (eight files / 192 KB total, no truncation).
  Queued turns retain their own item/context snapshot. Navigator links prefill, never auto-send.
  Collections use README titles/overviews/status, shared tags (inherited by supporting project
  documents), and the People roster's name aliases. People attribution uses explicit metadata or
  unambiguous document names; a passing mention or ambiguous first name does not assign a document.

## Pulse and constellation

- **`static/pulse.html`** - Vault Pulse telemetry page (project constellation first, then
  AI activity, live vault stats and hot files); polls `/api/pulse`. `/constellation` serves the same file, which hides
  everything but the constellation when it sees that path

- **Project constellation uses explicit signals, and its geometry means something.** Distance
  from the centre is time since anything in the project folder last changed, so rings read
  `today / 7 days / 30 days / dormant` outward and neglect is literally visible as drift. Lines
  are the documented `## Related` links between project READMEs (drawn even when only one side
  wrote the pair), and projects are ordered so each connected group holds one contiguous arc.
  Colour is state: amber for active Assistant board items linked by `project_ref`, coral for a
  project still marked `active`/`blocked` after `STALE_AFTER_DAYS` untouched, cyan for edits in
  the last seven days, hollow for `paused`/`completed`. Size is board attention; brightness is
  recency. The search box hides every star that does not match all typed terms against the slug,
  title or any front-matter value (`keywords` in `/api/pulse`, flattened by `frontmatter_text`).
  Keep these proxies transparent rather than introducing an opaque AI-generated
  importance score.

- **Constellation layers and moons stay on explicit links.** Besides projects, `scan_vault`
  returns `entities`: person dossiers (`notes/people/*.md`), incidents (`incidents/*.md`) and
  top-level notes, as diamonds, triangles and squares (People and Incidents on by default, Notes
  off, remembered per browser). `entity_kind` keeps logs, index READMEs and calibration, promo,
  feedback, review and personal-growth pages off the map. A page links to a project when it
  wikilinks it or names a `projects/<slug>/` path, when the README wikilinks the page, or, for a
  person, when the README lists them under `people`/`owner`. Entities sit at the circular mean
  angle of the projects they link to, on the same recency rings. Zooming past `MOON_SCALE` shows each
  on-screen project's own pages (`pages`, newest 40) as clickable moons. Label text, ring labels
  and link widths divide by `view.scale`, so zooming enlarges the space, not the type.

- **The constellation drawer is a briefing, and review writes only frontmatter.** Selecting a star
  fetches `/api/constellation/<slug>` (`project_brief`): Overview, Current state, provenance,
  linked artefacts (capped per kind so tickets cannot crowd out docs) and recently changed files.
  **Review** walks board-linked, then drifting, then unverified projects (`reviewQueue`). Its
  buttons POST `/api/constellation/<slug>/review` (`review_project`): a status change bumps
  `last_updated`, while "I've read it, it's right" adds the profile's `reviewer` (`human:<slug>`) to `verified` and
  sets `stale_after` per the projects provenance spec. Neither touches the README body. `?focus=<slug>`
  deep-links a star; keys are `/`, `r`, `f`, `j`/`k`, `c` and `Esc`.

- **Ring radii scale with occupancy, and labels are rationed.** A busy "today" would otherwise
  collapse into a few pixels at the centre, so band edges are part evenly spaced and part
  weighted by how many projects sit in each; angles then relax apart, pushing harder on inner
  orbits where a radian covers less ground. Only the most notable few projects carry a resting
  label - the rest appear on hover - and labels are dropped where they would collide with
  another label or a board-flagged star.

- **Pulse is fluid.** Its `container-fluid` fills the window; do not cap `.pulse-main` or hide
  horizontal overflow to conceal layout errors. Verify at 1920px and 2560px as well as smaller sizes.

## Screenshotting a page

Never point a headless browser at `/`, `/assistant`, `/pulse` or `/office`. They hold an open SSE
connection and polling intervals, so the page never reaches a quiet network state,
`--virtual-time-budget` never settles and the capture hangs until killed - leaving orphaned Chrome
processes and a temporary profile directory behind. Build a throwaway static harness that imports
the real `shell.css` and the page's own styles, screenshot that, then delete it.
