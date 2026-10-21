# herdr-pwa

A phone dashboard for herdr agents, served over Tailscale; `README.md` says what and why.
There's no design doc: the code, tests and commit log are the truth.

## Checking your work

- Iterate with targeted tests (`uv run pytest tests/test_<area>.py`). Run `just check`
  once per tree state, at the end, after one `npm ci --ignore-scripts` per worktree.
- Read the gate to its last line: the first `All checks passed!` is ruff, not the gate.
  To keep the exit code: `just check > out.txt 2>&1; echo "EXIT=$?" >> out.txt`.
- The gate and browser harnesses are heavy: one at a time (`pgrep -f "just check"` first),
  under `nice -n 19`. Reproduce a flake by stressing the test, not the machine.
- Fix the code; never widen a limit in `ruff.toml` or `pyproject.toml`.
- For a change under `web/templates` or `web/static`, run the `phone-look` skill before and
  after, and make the sheet. The owner checks it on a real phone before merge.
- `tests/fixtures/ledger.json` is the first implementation's decisions, frozen. Never edit
  a verdict: a deliberate divergence is a strict xfail with its reason in `DIVERGENCES`
  (`tests/test_ledger.py`).
- Re-pin an input hash (and `LEDGER_SHA256`) only when a capture was scrubbed or replaced
  and no verdict changes. Remove a row whose input isn't in the repo.

## Writing

- Comments are one line and say why, citing the outside source when there is one.
  Public API: one-line Google docstring.
- Commit messages: a subject of 72 characters or fewer, then a body of two sentences,
  five at most (the commit-msg hook checks). When a decision had alternatives, name the
  rejected one on a `Rejected:` line.
- Write state-dir files through `statedir.py`, which makes them 0600.
- No ticket ids, other projects, or anyone's machine, login or tailnet, anywhere in the
  repo or its commit messages. Fixtures follow `tests/fixtures/README.md`.
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
- Keep a harness's credentials where it put them, never in a fixture, log or test home.
- Starting a harness from an agent pane, strip the env as `scripts/live_session.py`'s
  `clean_env` does, or its hooks overwrite your pane's session.
- After changing what a running service serves, restart it and run `herdr-pwa doctor`.
