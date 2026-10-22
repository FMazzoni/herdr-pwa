# herdr-pwa

I run several AI coding agents at once in [herdr](https://herdr.dev), a terminal
workspace where each agent lives in its own pane. The agents stall whenever they need me,
for a permission prompt, a question or a plan to approve. herdr-pwa is the phone app I
built so I can see which one is waiting, read what it's doing, and answer it from anywhere.
It's unofficial, for one person, and reachable only over your
[Tailscale](https://tailscale.com) network, called a tailnet.

<p>
  <img src=".github/images/agent-answer-menu.png" width="200" alt="A terminal menu turned into tap targets">
  <img src=".github/images/feed-showcase.png" width="200" alt="The feed, with the agents that need you on top">
  <img src=".github/images/chat-report-table.png" width="200" alt="An agent's transcript as a chat, with a report">
  <img src=".github/images/files-diff.png" width="200" alt="A diff in the read-only file explorer">
</p>

## What it does

- A feed of every agent, with the ones that need you on top.
- Prompts, menus and plan approvals drawn as buttons, plus a box to type into.
- An agent's conversation shown as a chat, for Claude Code, opencode and
  [Hermes Agent](https://github.com/NousResearch/hermes-agent), or its live terminal.
- A read-only file browser and diff view for the repositories your agents work in.
- Buttons to start a new agent, stop one, or close a workspace.
- A notification when an agent needs you. A script on the machine can send its own with
  `herdr-pwa ping "tests passed"`, which shows as "Ping from" and the sender's name.

A tap is a keystroke with no undo, so the app reads the agent's screen again before each
one and refuses the tap if the screen has changed since your phone drew it.

## Requirements

- Linux, with Python 3.13 or later and [uv](https://docs.astral.sh/uv/).
- herdr 0.9.x on the same machine. The app speaks herdr's protocol version 22 and refuses
  to type into a herdr that speaks another, so a herdr protocol change needs an app update.
- Tailscale, with MagicDNS and HTTPS certificates turned on in its admin console.
- On an iPhone, iOS 16.4 or later for notifications.

## Running it

```sh
uv sync
export HP_TAILNET_LOGIN=you@example.com
export HP_PUSH_SUBJECT=mailto:you@your-real-domain
uv run herdr-pwa serve   # listens on 127.0.0.1:8772 and keeps running
# then, in another terminal:
uv run herdr-pwa pair    # publishes it on your tailnet and prints a QR code
```

`HP_TAILNET_LOGIN` is your Tailscale login, and the app refuses every other account.
`HP_PUSH_SUBJECT` is the contact address sent with each notification. Use a real domain,
because Apple rejects a placeholder and your iPhone then gets nothing.

On the phone, open the URL from the QR code and add it to the Home Screen. Open it from
there, tap "alerts", then "Turn on alerts here". Run `uv run herdr-pwa doctor` whenever
something looks wrong; it checks herdr, Tailscale and your settings.

## Security model

Tailscale is the only lock: it tells the app which account sent each request, so there is
no password. The app listens only on the machine itself, and only `tailscale serve`
passes requests to it from your tailnet. Before it types anything, the app writes a
record to its audit log, but it never stores the text you typed. [SECURITY.md](SECURITY.md)
says what it protects and how to report a problem.

## Developing

`just check` runs the linter, a type checker and the tests. The tests never touch a real
herdr; they run on screens and transcripts captured from real agents. `just live-session`
starts a throwaway herdr session, so you can try the app without touching your own agents.
The code was written by AI coding agents, directed by me, and issues are welcome.

## Licence

herdr-pwa is released under the MIT licence; see [LICENSE](LICENSE). It includes a copy of
htmx under its own 0BSD licence. The test screens in `tests/fixtures/foreign/collie/` come
from [collie](https://github.com/AltanS/collie), and its MIT notice sits beside them.
