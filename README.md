# herdr-pwa

I run several AI coding agents at once in [herdr](https://herdr.dev), a terminal
workspace where each agent lives in its own pane. They stall whenever they need me, for
a permission prompt, a question or a plan to approve, and the work waits until I'm back
at the desk. herdr-pwa is the phone app I built so I can see which one is waiting, read
what it's doing, and answer it from wherever I am. It's unofficial, for one person, and
reachable only over your [Tailscale](https://tailscale.com) network.

<p>
  <img src=".github/images/agent-answer-menu.png" width="200" alt="A terminal menu turned into tap targets">
  <img src=".github/images/feed-showcase.png" width="200" alt="The feed, with the agents that need you on top">
  <img src=".github/images/chat-report-table.png" width="200" alt="An agent's transcript as a chat, with a report">
  <img src=".github/images/files-diff.png" width="200" alt="A diff in the read-only file explorer">
</p>

## What it does

- **Feed.** Every agent in every workspace, sorted by what needs you, with what it's doing,
  and greyed out under a "can't reach herdr" bar when the phone loses touch.
- **Answer.** Permission prompts, menus, checklists and plan approvals are read off the
  screen and drawn as buttons. You can also type a prompt or press any key it offers.
- **Chat, Pane, Files.** The transcript as a chat (Claude Code, opencode and
  [Hermes Agent](https://github.com/NousResearch/hermes-agent)), the live terminal, and a
  read-only browser and diff for the git repos your agents work in.
- **Start and stop.** Open a new agent in a fresh tab, stop one, or close a workspace.
- **Push.** A notification when an agent needs you, or finishes something you haven't read.

## The interesting part: a tap is a keystroke with no undo

herdr's agents are terminal UIs, so there is no API for "answer this question". There's a
screen and there are keystrokes. Every tap travels this path, and any step can refuse:

1. **Find the agent by its session (its conversation), not its pane** (the terminal it
   sits in), because herdr reuses pane ids.
2. **Read the screen again.** If it changed since your phone drew it, the tap is refused.
3. **Send only keys that screen offers.** A key that would open an editor is never offered.
4. **Write the audit record, then send.** If the record can't be written, nothing is sent.
5. **Read back, never re-send.** "Sent, unverified" waits for an explicit "Send again anyway".

## Security model

- **Tailscale is the boundary.** The app trusts the `Tailscale-User-Login` header that
  `tailscale serve` adds: no password, no token. The one exception is `HP_ASSUME_LOGIN`,
  for local scripts, honoured only on a loopback Host. `herdr-pwa serve` listens only on
  loopback, so no other machine can reach it to forge the header.
- **Only on this machine's Tailscale name.** The header counts only when the request is
  addressed to the machine's MagicDNS name (the `*.ts.net` name Tailscale gives it), so a
  web page that points its own name at `127.0.0.1` gets nothing.
- **Pin your login** with `HP_TAILNET_LOGIN`. Without it, anyone your tailnet lets reach
  the machine can drive every agent, and startup warns you.
- **`pair` never uses Funnel** (Tailscale's public-internet sharing), refuses a node that
  has Funnel on, and never resets an existing `tailscale serve` config.
- **Anyone with a shell on the machine is trusted**, since they reach loopback too.
- **Agents run what they're told.** What a prompt asks for is left to the agent's own
  permission prompts.

The threat model and how to report a hole are in [SECURITY.md](SECURITY.md).

## Running it

You need Linux, Python 3.13+ with [uv](https://docs.astral.sh/uv/), herdr on the same
machine, and Tailscale with MagicDNS and HTTPS certificates on. The app pins herdr's
protocol and won't type into a version it doesn't know.

```sh
uv sync
export HP_TAILNET_LOGIN=you@example.com
export HP_PUSH_SUBJECT=mailto:you@your-real-domain  # a real address, or Apple drops pushes
uv run herdr-pwa serve   # listens on 127.0.0.1:8772
uv run herdr-pwa pair    # publishes it with tailscale serve, prints a QR
```

On the phone, open the URL, add it to the Home Screen, open it from there and turn
alerts on (on iPhone, push needs the Home Screen app and iOS 16.4+).

`uv run herdr-pwa doctor` exits non-zero if herdr is unreachable or speaks another
protocol, the service is older than the code, or the node's name is unknown. Settings
are `HP_*` environment variables (`src/herdr_pwa/settings.py`), and state lives in
`~/.config/herdr-pwa-v2`.

## How it fits together

Python (FastAPI, Jinja) on the server, htmx and a little plain JS on the phone, with no
build step. The layers in `src/herdr_pwa/` only point down, and import-linter holds them:

- `engine/` talks to herdr over its socket, reads screens and transcripts, ranks agents.
- `decisions.py` decides what every view and push shows, so they all agree.
- `writes/` is the path above: resolve, check, audit, send, read back.
- `web/` holds the routes, templates, the event stream and the push notifier.
- `files/` reads git in-process through libgit2, so no command a repo configures ever runs.
- `render/` turns Markdown, code and maths into element trees, so agent text never becomes HTML.

## How it's checked

`just check` runs lint, strict types, the tests, and a phone-width layout check in
headless Firefox; CI runs the same recipe.
Tests never touch a real herdr: they run on captured screens and transcripts in
`tests/fixtures/`. Every decision an earlier version of this app made over those screens
is frozen in `tests/fixtures/ledger.json`, and where this one deliberately disagrees, the
test is a strict xfail that says why.

For development you also need [just](https://just.systems), Node and Firefox. Once per
clone, run `npm ci --ignore-scripts` and install the git hooks the way
`.pre-commit-config.yaml`'s header says. `just live-session` starts a throwaway herdr.

## How it was built

<!-- OWNER: edit this paragraph -->
Most of the code was written by AI coding agents, each on its own branch, working to the
rules in [AGENTS.md](AGENTS.md). My part was deciding what to build, writing those rules,
reviewing and merging every change, and checking each screen on my own phone. Before
rewriting the first version, I recorded what it decided on dozens of real screens, so the
rewrite had to agree with it or explain why.

## Contributing

A personal project: issues and pull requests are welcome, handled when I can, no SLA.

## Licence

MIT, see [LICENSE](LICENSE). Vendored: htmx 2.0.10 (0BSD), in `src/herdr_pwa/web/static/`.
The screens in `tests/fixtures/foreign/collie/` come from
[collie](https://github.com/AltanS/collie) and keep its MIT notice beside them.
