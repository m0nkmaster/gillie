# Finishing up

## Ideas doc (`notes/skill-auditor-ideas.md`)

A **small decision inbox for the user**, not a record of the auditor's thinking. Add an
item only when all of these are true:

- it requires the user's judgement, taste, permission or an external action;
- there is a concrete decision to make now, with a clear recommendation;
- the auditor cannot safely resolve it within its existing autonomy; and
- it is not merely something to watch for possible recurrence.

Do not add completed work, created-skill announcements, validation evidence,
historical narrative, migration notes, low-confidence speculation or "revisit
if this happens again" items. Put those in the audit log. It is valid to add no
ideas in a pass.

**Organise by topic, never by date.** This is a running list the user triages over
time, not a per-pass log. **Never add a `## From the YYYY-MM-DD pass` section** -
that turns it into an ever-growing log, exactly what it must not become. Fold
each new thought into the existing theme (or add a topic heading if none fits),
prune resolved items as you go, and don't duplicate one already sitting there.
Keep entries tight: one decision, the minimum context needed, the auditor's
recommendation, and explicit options where useful. Once the user answers or the
decision becomes moot, action it and remove it. Target fewer than ten open items;
if the file grows beyond that, triage and prune it before adding anything.

## Audit log (`notes/skill-audit-log.md`)

Append the per-pass record:

```markdown
## YYYY-MM-DD HH:MM
- **Reviewed:** <N> skills
- **Changed:** <one line each, or "none">
- **Created/merged/split/deleted:** <one line each, or "none">
- **Far-reaching move:** <the bigger thing you pursued this pass, or "none warranted" after a genuine review>
- **Ideas logged:** <count added to skill-auditor-ideas.md, or "none">
- **Watermark:** <epoch-ms> - current time at finish (`echo $(($(date +%s)*1000))`), not a hand-converted `list` time (`list` prints UTC; converting as local skews the watermark).
```

## Commit

Commit **only the files you changed** (never `git add -A`):

```bash
git add notes/skill-audit-log.md notes/skill-auditor-ideas.md <each skill file you touched>
git commit -m "skills: <short summary of what you improved>"
```

If you changed no skill file and found no decision only the user can make, commit the audit-log
entry and watermark only. Do not pad the ideas file.
