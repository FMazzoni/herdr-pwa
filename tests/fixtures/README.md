# Fixtures

These files are real output from herdr and from the harnesses, the agent programs that run
inside herdr's panes: Claude Code, opencode and Hermes Agent. Each was captured once, so the
tests can run without a live herdr.

## What was changed before committing

The repo is public, and these captures came from real sessions, so they were scrubbed of
names, ids and personal details. `tests/test_fixture_hygiene.py` checks that nothing real
slipped through. Captured files are as captured, apart from these changes:

- Home directories read `/home/user`, and scratch directories `/home/user/scratch`.
- Claude Code's banner names the subscription plan `Claude API`, and login-expiry notices
  are gone.
- Statuslines read `herdr-pwa [main]`, with the segments that showed plan usage removed.
- Session titles, project names, agent names and tracker ids are neutral stand-ins.
- Inside a drawn box or a right-aligned row, a replacement keeps the line's length. The
  parsers read some screens by column, so a shorter line would change what they see.
- Anthropic request ids read `req_SYNTHETIC…`. The signatures on the model's reasoning
  blocks (thinking blocks) are placeholders.
- In transcripts and captures, session ids read `5e55NNNN-0000-…` and other record ids
  `0000NNNN-0000-…`; agent, tool-use and message ids are synthetic too. One mapping covers
  every file and test, so records that point at each other still line up.

A few files were built by hand rather than captured. They follow the field names and record
order that the harness was seen to write, with synthetic ids, times and text. Two are the
negative cases `capture-idle-claude-prompt` and `capture-working-claude`, screens that must
not be read as a question; each one's `meta.json` says how it was built. Some of the logs in
`transcript/notices/` were built the same way. Those hold the notices Claude Code writes
into a transcript, such as a slash command you ran or a compaction.

`claude/idle-box-table.txt` keeps the layout of a live screen: a reply's numbered box table
above the empty input box. Its text, cell by cell at the same widths, and its statusline are
made up.

Other captured files keep their real structure, but their prose was replaced with made-up
text. These are `transcript/opencode/error-rows.json`,
`transcript/notices/{notices,api-errors}.jsonl`,
and any capture whose `meta.json` says so.

The rest of `transcript/opencode/` is throwaway sessions run for these tests in scratch
folders: on free models with a store of their own, and on togetherai for the per-model rows.
opencode now writes only its SQLite store, so its session, message and part folders are
those sessions' rows written out as opencode 1.1 laid out its files, ids put back in each record.

## What's here

| path | what it is |
|---|---|
| `snapshot.example.json`, `agent-list.example.json`, `herdr-api-schema.json` | herdr's wire format |
| `capture-<state>-<shape>/` | one pane, read every way herdr offers |
| `ansi/`, `rig/` | screens that once showed a real bug |
| `hermes/` | Hermes Agent screens as herdr reads them. |
| `claude/` | Claude Code screens kept on their own, without a full capture's reads |
| `transcript/` | Chat history from Claude Code, opencode and Hermes stores, plus the notice and error records Claude Code writes. |
| `foreign/collie/` | Claude Code screens from another project |
| `ledger.json` | the golden record, described below |
| `parsed-corpus.json` | what every screen parses to |
| `elapsed.json` | how a span of time is worded |

The herdr files describe the messages herdr sends over its socket. The app's models in
`src/herdr_pwa/engine/generated/` are generated from `herdr-api-schema.json`.

Each `capture-<state>-<shape>/` folder holds one pane, as each of herdr's read requests
returned it. The state is blocked, idle or working, and the shape is what the screen shows,
such as `permission-prompt`. Its `meta.json` says how it was taken and on which
versions of herdr and the harness. `tests/test_corpus.py` says what each one must parse to.

`ansi/` holds screens read with their colours, and `rig/` holds screens drawn by the fake
herdr in `tests/catalogue.py`, which the layout check and screenshots run against. Each file is kept because a real bug showed up
on it.

`foreign/collie/` holds screens copied byte for byte from
[collie](https://github.com/AltanS/collie), under its MIT licence, which sits beside them
as `LICENSE`. They aren't scrubbed, because they are collie's files as published. Seven of
collie's screens were left out, because they showed their author's email or home directory.

`elapsed.json` is a table of time spans and how the app words them, such as "4m 12s". The
Python and JavaScript tests both read it, so the server and the phone word time the same way.

## The golden record

`ledger.json` records the decision expected on each of these screens and on some sample
agent lists. A decision says what kind of question a screen holds, which answers it
offers, or how a list of agents is sorted and labelled. `tests/test_ledger.py` makes the
code take each decision again and compares its answer with the recorded one.

- Never edit a recorded decision, because then the test would prove nothing. If the
  code deliberately decides differently, add the case to `DIVERGENCES` in
  `tests/test_ledger.py`, with the reason. The test then expects that case to fail, and it
  fails loudly if the case ever starts passing.
- The test also pins a hash of `ledger.json` and of every input it reads, so a silent edit
  is caught. Each input's hash is the `sha256` field of its row in `ledger.json`. Update it
  only when its capture was scrubbed or replaced and no decision changed. Record it under `meta.rehashed` in `ledger.json`, then update
  `LEDGER_SHA256`, the test's hash of the ledger itself.
- If a decision's screen isn't in the repo, remove that decision rather than keep it
  untested. Record it under `meta.pruned`.

## Regenerating

If herdr's protocol changes, capture its new schema and regenerate the models. This needs
`herdr` on your `PATH`:

```sh
uv run python scripts/generate_models.py --capture
```

If you deliberately change how screens are parsed, regenerate `parsed-corpus.json`:

```sh
uv run python -m tests.test_parsed_corpus
```

Then read the diff. Every entry that changed should be one your change meant to change; any
other is a screen your change broke.

## Adding a capture

1. Make the state happen in a pane you started yourself, in the throwaway `hp-test` session
   that `just live-session` starts. Never capture from someone else's session.
2. Save herdr's answers to each of its read requests into `capture-<state>-<shape>/`. Copy
   the file layout of an existing capture, such as `capture-blocked-plan-approval/`, and
   write a `meta.json` like its one. Read every file before committing, because panes can
   hold passwords and real names.
3. Add what it must parse to in `tests/test_corpus.py`, and regenerate `parsed-corpus.json`.
4. Run `uv run pytest tests/test_fixture_hygiene.py`. It fails on any email address, home directory,
   request id or commit id of this repo that isn't a placeholder.
