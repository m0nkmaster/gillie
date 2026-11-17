---
name: chat-ideas
description: Write the ideas a new Assistant chat offers, from this workspace's priorities, projects and tools, to `.roosta/state/assistant/chat-ideas.json`. Use on its schedule, or when the user asks to refresh their chat ideas.
---

# Chat ideas

Write six ideas for the user to click when they start a new chat. They stay up for a week, so each must still make sense in a week.

Read `PROFILE.md`, `PRIORITIES.md`, the `projects/*/README.md` titles and statuses, `TOOLS.md` and `.roosta/tasks.yaml`. Nothing else, and never `private/` or secrets.

Each idea is one sentence the user could send as-is, under 120 characters, in their language variant:

- **Durable.** Name standing things - a priority, an active project, a connected tool - never a date, a deadline, a ticket, a meeting, a count or anything that changes day to day.
- **Varied.** One or two of each `kind`: `focus`, `project`, `tool`, `remember`, `build`, `learn`.
- **Doable.** Only what Roosta does: read, draft (sent only on approval), remember, remind, and build pages, skills and workers.

A near-empty workspace gets setup ideas ("Help me write down my priorities", "Connect the tools I use").

Replace the file:

```json
{"generated": "<now, ISO 8601 with timezone>", "ideas": [{"kind": "focus", "text": "What should I focus on this week, given my priorities?"}]}
```

Return `details: {"count": N}` and no board items.
