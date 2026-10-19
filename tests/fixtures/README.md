# Fixtures

Captures of real herdr and harness output. Tests run against these, so the suite
needs no live socket and no running herd, which is what lets CI run it.

Each capture's `meta.json` says how it was taken. Blocked states were manufactured
by an agent in panes it created for the purpose and closed afterwards (the MCP
dialogs and the plan approval in the throwaway `hp-test` session); two negatives,
`capture-idle-claude-prompt` and `capture-working-claude`, are built by hand on the
layout of captures from live agent panes, with synthetic text. Each file is what
herdr or the harness wrote except where a section or `meta.json` says what was
rewritten, and except for one scrub applied throughout: home directories read
`/home/user`, scratch directories `/home/user/scratch`, the plan name in Claude
Code's banner reads `Claude API`, a login-expiry notice is removed, statuslines
read `herdr-pwa [main]` with their plan-usage segments removed, and session titles,
project names, agent names and tracker ids are replaced by neutral ones. A scrub
keeps a line's length wherever the line sits in a drawn box or a right-aligned row.

## The wire samples

| file | captured | what it is |
|---|---|---|
| `snapshot.example.json` | herdr 0.8.0, protocol 19 | `herdr api snapshot`: 6 agents (5 idle, 1 working), 5 workspaces |
| `agent-list.example.json` | herdr 0.8.0 | `herdr agent list` |
| `herdr-api-schema.json` | herdr 0.8.0, protocol 19, schema_version 1 | `herdr api schema --json`, pretty-printed and key-sorted |

`src/herdr_pwa/engine/generated/` is generated from the schema. Regenerate both with
`uv run python scripts/generate_models.py --capture`, then read the diff.

## The interpret corpus (`capture-*`)

Every `capture-<state>-<shape>/` directory holds the same files:

| file | what it is |
|---|---|
| `visible.txt` | `herdr agent read <target> --source visible` |
| `recent.txt` | `--source recent --lines 25` (25 is a cap: `--lines 200` took 7s and once killed herdr mid-read) |
| `recent-unwrapped.txt` | `--source recent-unwrapped --lines 25` |
| `detection.txt` | `--source detection` |
| `visible.ansi` | `--source visible --format ansi`, where the capture has one |
| `explain.txt` / `explain.json` | `herdr agent explain <target>`: the matched rule, its region and priority, and the evidence |
| `agent-get.json` | `herdr agent get <target>` |
| `meta.json` | provenance: target, timestamp, herdr version, protocol, manifest version, observed `agent_status` |

`tests/test_corpus.py` runs every capture through `BlockedPrompt.parse` in all four
sources and holds its provenance to the versions it declares.

| capture | status | why it is in the corpus |
|---|---|---|
| `capture-blocked-askuserquestion-menu` | blocked | The AskUserQuestion menu: five numbered options with a horizontal rule *between* options 4 and 5. |
| `capture-blocked-multiselect-checklist` | blocked | A multiSelect AskUserQuestion: six rows, a `[ ]`/`[✔]` marker on five, a description line between each pair, and a tab strip above the question. One row is already ticked, so a parser that assumes `False` fails. |
| `capture-blocked-permission-prompt` | blocked | The y/n permission shape for a Bash command, drawn as 1/2/3. The question drops the command. |
| `capture-blocked-permission-write` | blocked | File-write approval. The diff gutter's line number (`  1 hello`) is a decoy for option parsing, and its `╌` rules break the question. |
| `capture-blocked-plan-approval` | blocked | The ExitPlanMode approval: three rows under `Would you like to proceed?`, a `shift+tab to approve with this feedback` sub-line and a `ctrl+g to edit in Vim · <path>` footer, either of which once hid the whole menu. |
| `capture-blocked-trust-folder` | blocked | "Do you trust this folder?", shown before an agent session exists (`agent-get.json` has no `agent_session`). |
| `capture-blocked-opencode-*` | blocked | opencode's permission prompt, question, multi-select and tabbed question set. herdr records no manifest for opencode panes. |
| `capture-idle-numbered-recap` | idle | **Negative.** A finished transcript that ends on a `1./2./3.` list. Must not become tap targets. |
| `capture-idle-claude-prompt` | idle | **Negative.** A numbered recap with prose after it. Must not become tap targets. Built by hand; see its `meta.json`. |
| `capture-working-claude` | working | **Negative.** Mid-work; nothing is being asked. Built by hand; see its `meta.json`. |

A finished transcript that *ends* on a numbered list is still read as a menu. It is
carried as `xfail(strict=True)`, so the suite goes red the moment it is fixed.

What could not be captured:

- **`done`**: an agent given work in a pane that was never focused went
  `working → idle` with no `done` between, so driving it from the CLI appears to
  count as seen.
- **A blocked free-text prompt**: every free-text state tried (AskUserQuestion's
  "Type something", `Tab to amend`, a Bash call waiting on stdin) resolved to the
  idle prompt box, not to `blocked`.

### The soft-wrap pair (`capture-*-wrapped`)

The same two shapes taken in a 58-column pane instead of 158, so option labels wrap.
`capture-blocked-permission-prompt-wrapped` wraps option 2 so that the host being
allowlisted lands on the next row; `capture-blocked-askuserquestion-wrapped`
truncates four of six labels. All four sources agree byte for byte; the harm is that
the parser reads one row at a time. `herdr_pwa.panetext.printed_whole` recognises it,
and `tests/test_panetext.py` runs it over the whole corpus, where the hard part is
not flagging `capture-blocked-askuserquestion-menu`, whose description rows look
exactly like a wrap.

### The MCP elicitation dialogs (`capture-blocked-mcp-elicitation-*`)

herdr 0.9.0, protocol 22, Claude Code 2.1.302, from a throwaway stdio MCP server
handed to claude with `--mcp-config <file> --strict-mcp-config`. herdr calls all four
`blocked` through `mcp_elicitation_prompt`. The dialog is a title
(`MCP server "<name>" requests your input`), the server's message, one row per
field, an `Accept    Decline` row, and a footer with no Enter hint.

| capture | what it is |
|---|---|
| `…-confirm` | One required boolean field, cursor on it. |
| `…-accept` | The same after Space and Down: field ticked, cursor on Accept/Decline. |
| `…-form` | A required string, a required enum, an integer and a boolean. The message has no question mark. |
| `…-form-enum` | The same form with the enum opened by Right: an unnumbered radio list. |

Enter on a field moves to the next field and does not submit, and a text field can
only be filled by typing at the terminal.

### `detection` is not clean prose

`detection.txt` is byte-identical to `visible.txt` in every capture, and
`ansi/claude-blocked-bash-permission.detection.ansi` shows the detection source
returning the whole pane, every box frame intact, with no escape bytes at all. So
`HerdrClient.mirror_read` tries detection for a blocked agent and falls back to the
visible read unless the answer really is prose; `test_detection_equals_visible` is
the tripwire for the day herdr changes that.

## Colour reads (`ansi/`) and the rig's screens (`rig/`)

`ansi/` holds three `--format ansi` reads of one claude pane: idle, blocked on a Bash
permission prompt, and that prompt through `--source detection`. Between them they
carry 24-bit, 256-colour, bold, dim, italic and background SGR runs.

`rig/` holds screens the catalogue's fake runner draws, kept because a real defect
was seen on them:

| file | what it is |
|---|---|
| `rig/blocked-build-scrollback.ansi` | Coloured build steps with nothing asked. A card must say "Open to see what it's asking" over it. |
| `rig/opencode-scrollback-list.ansi` | Written by hand in opencode's palette: a message ending on a numbered list whose second row is an inline code span drawn in the colour opencode gives a focused row. The colour cursor counts only inside the dialog that ends the screen, so this screen earns no digits. |
| `rig/claude-statusline.ansi` | Written by hand on the layout of a Claude Code pane: the input box, a two-row statusline under it (a user script's `\|`-separated line in dim, green, yellow, red, cyan and bold magenta, then the harness's mode row). The stats strip draws these rows in place of its summed facts. |
| `rig/claude-statusline-narrow.ansi` | The same statusline on a 58-column pane, where the terminal cut the row mid-field. The strip shows what the screen shows. |
| `rig/claude-statusline-powerline.ansi` | Written by hand in a powerline style: truecolour foreground and background per segment, powerline arrows (U+E0B0) between them and Nerd Font icons (private use) in them. The strip keeps the words and foregrounds, draws no background, and the glyphs a phone cannot draw become the gaps between pieces. |
| `rig/claude-statusline-bars.ansi` | Written by hand in a bar style: a block meter, `│` and `•` separators, emoji icons, 256-colour fills. Every glyph here renders on a phone, so every one is kept. |
| `rig/claude-statusline-rows.ansi` | A statusline of three rows and the mode row, as a script printing several lines leaves it. The strip's line is the first row; the sheet has all four. |

## hermes (`hermes/`)

hermes screens as herdr reads them: a blocked approval, a clarify question, a slash
command's confirm, a stopped approval, and an answered dialog herdr still flags.

## Chat history (`transcript/`)

| file | what it is |
|---|---|
| `transcript/claude/*.jsonl` | Two Claude Code transcripts, one of a short probe session and one of an edit to this repo's README. Small, and between them they cover every record shape `engine/transcript.py` branches on. Three tool, agent and skill listing attachments were removed from the probe session and its parent chain re-linked. |
| `transcript/lineage/` | Head records that show how a copied conversation names its parent; see its README. |
| `transcript/opencode/{session,message,part}/` | Two opencode sessions from its file store: a 13-message build session, and a 6-message subagent session with an errored tool part. Ids, timings, token counts, models and the shape of every record are as opencode wrote them; every prompt, reply, tool input and output is replaced with synthetic content about this repo. |
| `transcript/opencode/schema.sql`, `db-rows.json`, `error-rows.json`, `per-model-rows.json` | opencode's SQLite schema (as captured) and rows read from it, including every failed answer found (a 429, two interrupts, a context overflow, an input validation error), with paths, prompts, a command and request ids replaced by neutral ones. Every errored row records zero tokens and zero cost. |
| `transcript/hermes/schema.sql` | hermes's SQLite schema; the tests write their own rows. |
| `transcript/notices/notices.jsonl` | Records Claude Code writes as user records that nobody typed: task notifications, subagent hand-backs, messages from other sessions, and system-reminder attachments. Frames, tags, origins, ids and timings are as written; report and message bodies, one assistant answer and one prompt are replaced with short synthetic text. |
| `transcript/notices/api-errors.jsonl` | One of every API error shape seen (`invalid_request` "Prompt is too long", session and weekly limits, a 529, `authentication_failed`, `model_not_found`), all `type: assistant` from model `<synthetic>`, set among notices in the order a full context was seen to refuse. The notices among them carry the same synthetic bodies as `notices.jsonl`. |
| `transcript/notices/slash-commands.jsonl` | Five slash commands as Claude Code writes them: the caveat, the command record, then its output. |
| `transcript/notices/compaction.jsonl` | Two `/compact` runs: the bare line as sent, the boundary, the summary (body invented), the caveat, the command and "Compacted". |
| `transcript/notices/system-commands.jsonl` | Slash commands Claude Code writes as `type: system` records. |
| `transcript/notices/command-stderr.jsonl` | **Built, not captured**: no log held a failing command, so its stderr takes the stdout shape. |
| `transcript/notices/spawned.jsonl` | A session starting background agents, and the notices they sent back. |
| `transcript/notices/queued.jsonl` | **Built, not captured**, on the measured shapes of prompts sent while a turn ran: the queue's `enqueue`, then the `absorbed_mid_turn` removal and the `queued_command` attachment that is the only record of the prompt, or `dequeue` and a user record whose `promptSource` is `queued`, or `popAll` when the person pulls the queue back to edit; a paste in its `pasted_content` tags, a notice delivered the same way, and one prompt still waiting at the end. Field names, order and the reminder text are as Claude Code writes them; ids, times and every prompt are synthetic. |
| `transcript/notices/queued-repeat.jsonl` | **Built**, the same way: one prompt's words sent three times (typed; queued and absorbed; queued and still waiting), so a match on the words is shown never to merge two prompts. |
| `transcript/notices/shell-commands.jsonl` | **Built** on the measured shape of `!` commands: `<bash-input>` alone, then `<bash-stdout>` and `<bash-stderr>` in the next record, with output, with only stderr, and with the harness's no-output line; commands and output are synthetic. |

## The event stream (`events/`)

Every frame from `events.subscribe` connections, one JSON object per line, with each
frame's wall-clock offset in the matching `.timing.tsv`. No test reads them; they are
the evidence for how the notifier uses events:

1. `events.subscribe` holds its connection open, where every other method hangs up
   after one reply.
2. `pane.output_matched` is a pattern trigger, not an output stream: it fires once
   when the pattern appears and not again until it has gone (`/clear` in
   `subscribe-mango-clear-retrigger`), and each firing carries a whole viewport.
3. `pane.scroll_changed` never fired in 140s of subscription.
4. `events.wait` accepts only agent status matches, though the schema offers 26
   (`wait-pane-output-changed-unsupported`).

## Golden files

- `ledger.json`: every decision the previous implementation made over these screens
  and over herd snapshots. `tests/test_ledger.py` recomputes each one and pins the
  file's hash and each input's; a deliberate divergence is a strict xfail with its
  reason. Verdicts are never edited; input hashes are re-pinned when a capture is
  scrubbed or replaced, and `meta.rehashed` says so. Every row reads a file in the
  repo; rows for inputs never shipped were removed (`meta.pruned`).
- `parsed-corpus.json`: what every screen parses to. Regenerate after a deliberate
  grammar change with `uv run python -m tests.test_parsed_corpus`, then read the diff.

## `foreign/collie/`: claude screens vendored from collie

The 46 `claude--*.txt` pane captures under `foreign/collie/` are byte-for-byte
copies of `web/src/fixtures/panes/` in
[AltanS/collie](https://github.com/AltanS/collie) at commit
`2031ed7508703a9b8b6703ed07d8eeaf9f608468` (the ledger's
`meta.foreign_sources.collie`), shipped under collie's MIT licence, whose notice is
kept beside them in `foreign/collie/LICENSE`. They are not scrubbed: they are
collie's, as published. `tests/test_ledger.py` holds them to the ledger's rows, and
`tests/test_parsed_corpus.py` walks them with the rest.

## Adding a capture

1. Manufacture the state in a pane of your own in the `hp-test` session
   (`just live-session`), never on an agent you did not start.
2. Save the reads above into `capture-<state>-<shape>/`, then read every byte: a pane
   can carry a password, or a path that names someone.
3. Say what it must parse to in `tests/test_corpus.py` (`MENUS` or `NEGATIVES`, and
   the version overrides if its versions differ), add a row to the table above saying
   why the shape is worth keeping, and regenerate `parsed-corpus.json`.
