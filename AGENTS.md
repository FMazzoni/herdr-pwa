# herdr-pwa

A phone dashboard for herdr agents, served over Tailscale; `README.md` says what and why.
There's no design doc: the code, tests and commit log are the truth.

## Checking your work

- Iterate with targeted tests (`uv run pytest tests/test_<area>.py`). Run `just check`
  once per tree state, at the end, after one `npm ci --ignore-scripts` per worktree.
- Read the gate to its last line: the first `All checks passed!` is ruff, not the gate.
  To keep the exit code: `just check > out.txt 2>&1; echo "EXIT=$?" >> out.txt`.
- The gate and browser harnesses are heavy, and the owner works on the same machine. Run
  one at a time (`pgrep -f "just check"` first), under `nice -n 19`. Reproduce a flake by
  stressing the test, not the machine.
- Fix the code; never widen a limit in `ruff.toml` or `pyproject.toml`.
- For a change under `web/templates` or `web/static`, run the `phone-look` skill before and
  after, and make the sheet. The owner checks it on a real phone before merge.
- `tests/fixtures/ledger.json` records the expected decision on each captured screen and
  agent list (its verdict), and `tests/test_ledger.py` checks the code
  still agrees. Never edit a verdict, or the check proves nothing. A deliberate divergence
  goes in `DIVERGENCES` with its reason, which makes that case a strict xfail: an expected
  failure that fails the run if it starts passing. `tests/fixtures/README.md` explains more.
- Re-pin an input hash (a row's `sha256`, and `LEDGER_SHA256`, the test's hash of the
  ledger file) only when a capture was scrubbed or replaced and no verdict changes; note it
  in `meta.rehashed`. Remove a row whose input isn't in the repo.

## Writing

- Comments are one line and say why, keeping the consequence (the effect, not the
  narration), and cite the outside source when there is one.
  Public API: one-line Google docstring.
- Lay out a new or reorganized module in one order: its constants and the types it deals
  in, then public functions, then private helpers in the order they are called.
- Commit messages: a subject of 72 characters or fewer, then a body of two sentences,
  five at most (the commit-msg hook checks). When a decision had alternatives, name the
  rejected one on a `Rejected:` line.
- Write files in the state dir (`HP_STATE_DIR`, by default `~/.config/herdr-pwa`)
  through `statedir.py`, which makes them private to their owner (mode 0600).
- No ticket ids, other projects, or anyone's machine, login or tailnet, anywhere in the
  repo or its commit messages. Fixtures follow `tests/fixtures/README.md`.
- Public docs describe provenance and process at a high level: no version history, no
  breakdown of who did what.
- Change `README.md`, `AGENTS.md` or `SECURITY.md` only when asked. Notes, scratch and
  reviews go in `.agents/`, which is gitignored except `.agents/skills/`.

## Branches and the herdr test rig

- Work in a worktree: `git worktree add .worktrees/<name> -b <name>`. Leave the main
  checkout alone: the running service may be installed from it. After the merge, remove
  the worktree and the branch. Don't push or merge into `main` unless asked.
- Drive only the throwaway `hp-test` session (`just live-session`), and only agents you
  started there. herdr's default session belongs to whoever runs it.
- Serve builds on port 8772 (`HP_PORT`). Leave other servers and any `tailscale serve`
  config as you found them.
- A harness is an agent program herdr runs, such as Claude Code. One started from inside an
  agent pane inherits that pane's environment, so its hooks report its session as your
  pane's. Strip the environment as `scripts/live_session.py`'s `clean_env` does: every
  `HERDR_*` and `CLAUDE_CODE_*` variable, plus `CLAUDECODE`, `CLAUDE_PID`, `CLAUDE_EFFORT`
  and `AI_AGENT`.
- Keep a harness's credentials where it put them, never in a fixture, log or test home.
- After changing what a running service serves, restart it and run `herdr-pwa doctor`.
