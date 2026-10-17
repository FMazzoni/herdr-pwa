# herdr-pwa

A self-hosted phone dashboard for watching and driving herdr agents, served over
Tailscale. `README.md` covers what it does, its security model and how to run it;
`docs/invariants.md` holds every rule the app keeps and what was rejected.

## Verification

`just check` is the gate, and CI runs exactly that recipe: ruff format, ruff check,
basedpyright (strict, `Any` is an error), complexipy (cognitive complexity at most 10),
pytest, the JS tests, the CSS token check and the geometry gate. Read its output to the
last line; the first `All checks passed!` is ruff, not the gate. To keep the real exit
code: `just check > out.txt 2>&1; echo "EXIT=$?" >> out.txt`.

Iterate with targeted tests (`uv run pytest tests/test_that_area.py`) and run the gate
once per tree state, at the end. The gate and the browser harnesses are heavy: run one
at a time (`pgrep -f "just check"` first), under `nice -n 19`, and reproduce a timing
flake by making the test itself the stress rather than loading the machine.

Behaviour is pinned by `tests/fixtures/ledger.json`, a golden record of the decisions
the previous implementation made. Its verdicts are never edited; a deliberate
divergence is a strict xfail with a reason in `tests/test_ledger.py`. Its input hashes
may be re-pinned when a capture is scrubbed or replaced, as long as no verdict changes,
and a row whose input is not in the repo is removed rather than kept untested.

## Writing code here

- Comments are one line and say why, citing the outside source when there is one.
- Public API gets a one-line Google-style docstring summary; private code needs none.
- Longer explanations go in the few hand-written files under `docs/`. Agents write
  there only when launched with `HP_DOCS_WRITE=1`; `.claude/hooks/guard-docs.py`
  enforces it for Claude Code.
- The repo stands on its own: code, comments, fixtures and commit messages name no
  ticket ids, other projects or anyone's machine. Fixtures follow
  `tests/fixtures/README.md`.
- Notes, scratch and reviews go in `.agents/`, which is gitignored except for
  `.agents/skills/`, the skills shared with every agent (`.claude/skills` links there).

## Branches and worktrees

- Work on a branch in its own worktree under `.worktrees/` (gitignored):
  `git worktree add .worktrees/<name> -b <name>`. A running service may be an
  editable install of the main checkout, so edits there go live.
- After the merge and the post-merge checks, `git worktree remove .worktrees/<name>`
  and `git branch -d <name>`.

## herdr: the test rig

- Drive only the throwaway `hp-test` session from `just live-session`, and only agents
  you started there. herdr's default session belongs to whoever runs it.
- Serve builds on the default port (8772, `HP_PORT`), and leave any other server and
  any `tailscale serve` config as you found them.
- Keep a harness's credentials where the harness put them: never copy them into a
  fixture, a log or a test home.
- A harness started from inside an agent pane inherits `HERDR_PANE_ID`, and its
  lifecycle hook then overwrites that pane's reported session. Strip herdr's
  environment when spawning one: `env -u HERDR_PANE_ID -u HERDR_SOCKET_PATH -u
  HERDR_ENV -u HERDR_TAB_ID -u HERDR_WORKSPACE_ID <harness> ...`.
- After a change to what a running service serves, restart it and run
  `herdr-pwa doctor`.

## State

Settings come from `HP_*` env vars (`src/herdr_pwa/settings.py`); the state dir is
`~/.config/herdr-pwa-v2` by default. Files the app owns there are written 0600.
