# Intake sources - preflight and what to read

Phase 1 detail for the gardener. Every connected tool, its sign-in check and its quirks are listed in the workspace `TOOLS.md` and `.roosta/connectors.yaml`.

## Preflight - test before declaring anything unavailable

Never guess that a tool is down. Run each connector's cheap `check` command (from `.roosta/connectors.yaml`, or the check `TOOLS.md` gives) and mark a source unavailable only if that command genuinely fails. On a real failure, log the exact failing command, not a blanket "unavailable".

## What to read per source

Read the sources `TOOLS.md` lists, through the lens of `PRIORITIES.md`:

- **Chat and email** (the channels `TOOLS.md` says matter): decisions, new information, shifts in direction; what generates energy versus what fell flat; commitments made and questions left unanswered. To refine a card for a thread, use its existing id (see the board contract's **Updating an existing card**).
- **Tickets:** new tickets, status changes, completed work; whether work is piling up somewhere.
- **Task tool:** current task state - done, new, overdue. Never mirror it into the workspace.
- **Wiki and docs:** recently updated pages relevant to active projects.
- **Local artefacts:** exported AI sessions, analysis folders, dated notes from deep sessions. Did a session produce durable knowledge still scattered across files?

Don't commit during intake. Jot raw observations into `notes/.garden-scratch.md` (overwrite each run); delete it in the finishing step.
