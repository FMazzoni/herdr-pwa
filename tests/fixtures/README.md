# Fixtures

These are real output from herdr and from the harnesses (the agent CLIs: Claude Code,
opencode and Hermes Agent), captured once, so the tests need no live herdr.

## What was changed before committing

Everything is as captured, apart from this scrub:
- Home directories read `/home/user`, and scratch directories `/home/user/scratch`.
- Claude Code's banner names the plan `Claude API`, and login-expiry notices are gone.
- Statuslines read `herdr-pwa [main]`, with their plan-usage segments removed.
- Session titles, project names, agent names and tracker ids are neutral stand-ins.
- Inside a drawn box or a right-aligned row, a scrub keeps the line's length.
- Anthropic request ids read `req_SYNTHETIC…`, and thinking-block signatures are placeholders.

A few files were built by hand, following the field names and record order the harness
was seen to write, with synthetic ids, times and text: the negatives
`capture-idle-claude-prompt` and `capture-working-claude` (their `meta.json` says how),
and some of the notice logs in `transcript/notices/`. Some captured files keep their
shape with the prose made synthetic: `transcript/opencode/`,
`transcript/notices/{notices,api-errors}.jsonl`, and any capture whose `meta.json` says so.

## What's here

| path | what it is |
|---|---|
| `snapshot.example.json`, `agent-list.example.json`, `herdr-api-schema.json` | herdr's wire format. `engine/generated/` is generated from the schema. |
| `capture-<state>-<shape>/` | One blocked, idle or working pane, read through every source herdr offers. `meta.json` says how it was taken and on which versions. `tests/test_corpus.py` says what each one must parse to. |
| `ansi/`, `rig/` | Coloured reads, and screens drawn by the test catalogue's stand-in for herdr, kept because a real bug showed up on them. |
| `hermes/` | Hermes Agent screens as herdr reads them. |
| `transcript/` | Chat history from Claude Code, opencode and Hermes stores, plus the notice and error records Claude Code writes. |
| `foreign/collie/` | Claude Code screens copied byte for byte from [collie](https://github.com/AltanS/collie) under its MIT licence (`LICENSE` beside them). These aren't scrubbed: they're collie's, as published, less seven screens that showed their author's email or home directory. |
| `ledger.json`, `parsed-corpus.json` | The golden record (below), and what every screen parses to. |
| `elapsed.json` | How a span of time is worded. The Python and JS tests share this table. |

## The golden record

`ledger.json` holds every decision an earlier version of this app made over these screens
and over agent snapshots. `tests/test_ledger.py` recomputes each one and pins the file's
hash and every input's hash.
- Verdicts are never edited. A deliberate disagreement is a strict xfail in `DIVERGENCES`.
- An input hash is re-pinned only when its capture is scrubbed or replaced and no verdict
  changes, and a row whose input isn't in the repo is removed, not kept untested. The
  `meta.rehashed` and `meta.pruned` keys in `ledger.json` record both.

## Regenerating

Schema and models: `uv run python scripts/generate_models.py --capture`. After a
deliberate grammar change: `uv run python -m tests.test_parsed_corpus`. Read the diff.

## Adding a capture

1. Make the state happen in a pane you started yourself, in `hp-test` (`just live-session`).
2. Save the reads into `capture-<state>-<shape>/` with a `meta.json`, then read it all
   before committing. Panes can hold passwords and real names.
3. Add what it must parse to in `tests/test_corpus.py`, and regenerate `parsed-corpus.json`.
4. `tests/test_fixture_hygiene.py` fails on any email, home directory, request id or
   commit of this repo that is not a placeholder.
