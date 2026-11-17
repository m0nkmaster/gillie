# Checkpoints, gaps and historical backfill

`notes/learning-state.json` is the machine-readable record of what has been
completely inspected. `notes/learning-log.md` explains what was learned; it is
not the cursor.

## Source isolation

Track each source independently:

- AI history: `oc`, `cursor`, `cc`, `assistant` (always available)
- one key per connected chat or email source, named `<connector>:<channel-slug>` (for example
  `slack:team-leads`), plus `<connector>:chat-index` and `<connector>:dm:<id>` for tracked DMs

Never advance one source because another source succeeded. Capture `scan_end_ms`
before querying so material arriving during the pass falls into the next pass.

Each source has:

```json
{
  "live": {"checkpoint_ms": 0, "boundary_ids": []},
  "backfill": {"cursor_ms": 0, "boundary_ids": [], "complete": false},
  "open_gap": null
}
```

`boundary_ids` prevents timestamp ties from being skipped. Keep IDs processed at
the current live or backfill millisecond.

## Live scan

For each local AI source:

1. Run `python3 sessions.py status`. A missing store is unavailable, not an empty
   result.
2. List from `checkpoint_ms - 1` through `scan_end_ms` for that source. Skip IDs
   already recorded at the checkpoint millisecond.
3. Read and classify every returned conversation.
4. Advance that source to `scan_end_ms` only after listing and transcript reads
   complete. Record IDs exactly on the new boundary.

Use:

```bash
python3 sessions.py list <checkpoint-minus-1> \
  --source <source> --before <scan-end-plus-1> --json
```

For chat sources, paginate each channel or chat independently through `scan_end_ms`.
Advance only after all pages and relevant complete threads have been read.

If a source fails, leave its checkpoint unchanged and set `open_gap` with
`from_ms`, `first_seen_at` and the exact error. Never convert an
authentication failure into a successful empty scan.

A gap drains forwards, never all at once. Where the interval from the
checkpoint is wider than about a day, close it one bounded window at a time,
oldest first, and advance the checkpoint to the end of each window you
actually finished. A pass that reads part of an interval advances by that
part; it never throws the work away and leaves the checkpoint where it was.
Clear the gap once the checkpoint reaches `scan_end_ms`. An interval too wide
to finish in one pass is a reason to take the oldest window now, not a reason
to skip the source - skipping is what let the gap grow.

## Historical backfill

Live scanning takes priority. After it completes, process at least one historical
batch from one incomplete source, rotating sources between runs.

For local AI history, fetch 20 conversations newest-first from before the
source's backfill cursor:

```bash
python3 sessions.py list 0 --source <source> \
  --before <cursor-plus-1> --newest-first --limit <batch-plus-boundary-count> --json
```

`assistant` is the exception: the helper timestamps every conversation at the file's modification
time, so each live scan already enumerates the whole store and there is no older history to reach.
Its backfill is complete by construction - do not spend a rotation on it.

Skip recorded boundary IDs, read the next 20, then move `cursor_ms` to the oldest
processed timestamp and retain all processed IDs at that millisecond. Increasing
the command limit by the boundary count avoids hiding additional records that
share a timestamp.

For chat sources, page each channel or chat with bounded reads (the paging rules are in
`TOOLS.md`); where the workspace keeps a local archive of a channel, slice batches from it rather
than paging the live tool. Retain the oldest message timestamp per channel/chat. Mark backfill complete
only after the earliest accessible record is reached. Record any retention or permission boundary
explicitly; "complete" means all accessible history, not a claim that the provider retained
everything.

Backfilled conversations use the normal verification and promotion pipeline.
Log the source, batch window and remaining status. Do not advance a backfill
cursor if any conversation in that batch could not be read.

## Concurrent passes

Nothing locks this loop, and `learning-state.json` is last-writer-wins while the
learning log is prepend-only, so a scheduled pass and a manually started one
overlapping will each scan the same window, repeat the same backfill batch and
leave two entries for it. Before writing anything, re-read the newest log entry:
if its watermark falls inside this pass's window, another pass has already
covered it. Keep the earlier entry, record only the interval genuinely beyond its
watermark, and never re-promote a fact it already promoted.

## Recovery

If `learning-state.json` is absent or invalid, reconstruct the live checkpoints
from the latest trustworthy log entry. Start historical cursors at the same
point and mark every backfill incomplete. Never silently fall back to 24 hours.
