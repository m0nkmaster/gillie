# Intake checkpoints and backfill

`notes/garden-state.json` records what the gardener has fully digested. It
prevents a failed or interrupted pass from silently skipping learner output or
local changes.

## Ownership boundary

The learner owns conversation discovery, per-source gaps and transcript
backfill. The gardener consumes `notes/learning-log.md`; it never re-mines those
conversations. Historical discoveries appear as new learner log entries and
therefore enter the gardener's live stream normally.

## Tracked inputs

- **Learning log:** live checkpoint plus an older backfill cursor.
- **Git:** last commit completely inspected.
- **Local artefacts:** last modification-time window completely inspected.

External systems used to verify a concrete claim are selective evidence sources,
not exhaustive feeds. Record an unavailable verifier in `garden-log.md`, but do
not claim a completeness checkpoint for a ticket, chat or wiki source.

## Live intake

Capture `scan_end_ms` before reading.

1. Read every learning-log entry newer than
   `learning_log.live.checkpoint_ms`, including entries exactly on the boundary
   whose IDs are absent from `boundary_ids`.
2. Read every commit after `git.checkpoint_commit`.
3. Inspect relevant local artefacts modified after
   `local_artifacts.checkpoint_ms` and no later than `scan_end_ms`.
4. Digest, verify and write before advancing any checkpoint.

Advance each input independently only after its window is fully handled. If a
read fails or the pass stops partway through, preserve that checkpoint and set
its `open_gap` with the failing interval and reason.

A window you cannot finish is split, not skipped. Where an input's interval has
grown past about a day, take the oldest bounded slice of it, digest that slice
properly, and advance the checkpoint to the end of the slice you finished. Part
of an interval handled is part of the checkpoint moved. A checkpoint that has
not moved for several passes means the window is being declined rather than
worked, and the next pass takes its oldest slice first.

## Historical backfill

After live intake, process one bounded batch of older learning-log entries,
newest-first from `learning_log.backfill.cursor_ms`. Move the cursor to the
oldest fully digested entry and retain IDs at that timestamp. Mark complete only
at the beginning of the log.

Backfill old git or loose artefacts only when they predate the learner logs or
contain durable knowledge not represented there. Do not manufacture edits from
old operational noise. Record the inspected range even when no canonical change
is warranted.

## Recovery

If `garden-state.json` is absent or invalid, reconstruct:

- the live learning checkpoint from the newest learner watermark explicitly
  named as digested in `garden-log.md`
- the git checkpoint from the latest successful `garden:` commit
- the local-artifact checkpoint from that pass time

Start historical cursors from those points and mark backfill incomplete. Never
default to "recent" or advance to now merely because reconstruction is awkward.
