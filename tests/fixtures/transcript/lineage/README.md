# Records that show a conversation was copied

Claude Code keeps each conversation as a log file, one JSON record per line. Some actions
copy the conversation into a new log file and carry on there: `/fork`, resuming after
`/compact`, and handing a session off to run in the background. herdr learns the session id
when the agent starts and isn't told about the copy. So it may still report the old
session, whose log has stopped growing. To show you the live conversation, the app must
find the copy, and it does that by reading markers that Claude Code writes into the copy.

These files are single records from real logs, each holding one of those markers. The tests
build small logs around them. A version number in a file name, such as `2.1.283`, is the
Claude Code version that wrote the log. Ids and private text were scrubbed as
`tests/fixtures/README.md` describes.

## Why the app reads only the start of a log

A log can grow to many megabytes, and the app checks the other logs in the same project
folder each time it looks for a copy. So it reads only the head window: the first 64 KiB of
each file. If a record crosses that 64 KiB mark, the app reads on to the end of that one
record, up to 256 KiB further. A marker the app needs must therefore start in the first
64 KiB and end within the first 320 KiB.

## Captured 2026-08-20: a fork and a compaction

Each file is the first record carrying a `uuid` in a real log. The `uuid` is the field
Claude Code uses to identify each record.

| file | captured from | what produced it |
|---|---|---|
| `forked-head.json` | log `5e55001b….jsonl` | `/fork` |
| `compacted-head.json` | log `5e550009….jsonl` | resuming after `/compact` |

A copy names the session it was copied from in its `forkedFrom.sessionId` field. Claude
Code writes that field itself, so the app doesn't have to guess. Guessing would not work
anyway. One guess would match a copy to its parent because both begin with the same record
`uuid`. But the first record of `compacted-head.json` is a new `compact_boundary` record
with a new `uuid`, so it shares no first record with its parent.

The scrub replaced `message.content`, `content`, `compactMetadata` and `cwd`. They held the
author's own prose, token counts and directories, and the app's reader doesn't depend on
them. The fields the reader does use (`forkedFrom`, `sessionId`, `uuid`, `type` and
`timestamp`) are as Claude Code wrote them. The one exception is ids: every id in this
folder was replaced through a single mapping, so records that point at each other still
match.

## Captured 2026-10-17: chains of copies

A copy can itself be copied, so the app follows a chain from the session herdr names to the
newest copy. These records come from such chains. The scrub also replaced `message`,
`gitBranch`, `slug`, `requestId`, `serverClassifierRequest`, `promptId` and the
`preCompact*` tool lists. Every field the reader depends on is as Claude Code wrote it.

A background handoff marks its copy differently from a fork. Its records carry
`sessionKind: bg` and a `session_id` field, spelled with an underscore. That field names
the session that wrote the record, so on a copy it names the parent. This doc calls it the
parent's stamp.

| file | marker it holds |
|---|---|
| `bg-preserved-2.1.283.json` | a background copy's parent stamp |
| `fork-compact-boundary-2.1.289.json` | `forkedFrom` on a compaction record |
| `fork-continued-2.1.289.json` | the parent's stamp on a fork's newest record |

`bg-preserved-2.1.283.json` is the first record of a background handoff after the
compaction it carried over from its parent. It has `sessionKind: bg` and the parent's
stamp. The copy adds no compaction of its own.

`fork-compact-boundary-2.1.289.json` is the first record of a copy that a compaction wrote
in the foreground. It has `forkedFrom` and is a `compact_boundary` record, with no
`sessionKind`.

`fork-continued-2.1.289.json` is the newest record of that same copy, written a day after
its parent stopped. It carries the parent's stamp, so the session herdr reports kept
writing into the copy. The copy is smaller than its parent because compaction shrank it.

## Captured 2026-10-17: a stamp that the head window cuts through

These two records come from the 2.1.283 background handoff above. In that log, the
parent's stamp first appears on the compaction summary, the record that holds the
conversation so far in short. The compaction record ends at byte 55,572, and the summary
runs from there to byte 78,955. So the summary crosses the 64 KiB mark, and the app finds
the stamp only because it finishes reading a record that crosses the mark. The tests
rebuild that layout around these two records.

| file | marker it holds |
|---|---|
| `bg-compact-boundary-2.1.283.json` | `sessionKind: bg`, but no stamp |
| `bg-compact-summary-2.1.283.json` | the summary, with the parent's stamp |

The scrub is the same as for the chain records, plus `message.content`, which held 22,203
characters of summary prose, and `content`. The summary record also carries
`isCompactSummary` and `sessionKind: bg`.

The three `*-2.1.241.json` records (`bg-compact-boundary`, `bg-parent-stamp` and
`plain-fork-head`) are older records of the same kinds, scrubbed the same way. They keep the tests honest
about logs that older Claude Code versions wrote.
