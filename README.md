# herdr-pwa

A self-hosted phone dashboard for watching and driving herdr agents. It lists every
agent in the herd by how urgently it needs you, shows each one's chat, terminal and
files, and lets you answer a question, send a prompt or press a key from the phone. A
Web Push notification says when an agent stops and needs a person.

It runs on the machine herdr runs on and is reached only over your tailnet, through
`tailscale serve`.

## Security model

- **Tailscale is the boundary.** The app trusts the `Tailscale-User-Login` header that
  `tailscale serve` adds, and nothing else: there is no password and no token. It
  refuses to bind anything but loopback, so no other peer can send that header itself.
- **Pin the login.** Set `HP_TAILNET_LOGIN` to your tailnet login; without it any user
  your tailnet lets reach the node may drive every agent, and startup warns.
- **Writes are guarded.** Every write is same-origin, keyed on the agent's session (pane
  ids are reused), re-checked against a fresh read of the pane, and refused when the
  screen is not the one the phone was shown. Destructive shell input asks for a
  confirm on the server. Nothing is re-sent automatically.
- **Writes are audited.** Each prompt, key, start and close is recorded before it is
  sent, in a 0600 log under the state directory; a box that cannot record does not
  serve.
- **Pairing never uses Funnel**, and never resets an existing `tailscale serve`
  config.

`docs/invariants.md` lists every rule and what was rejected.

## Requirements

- Linux, Python 3.13 and [uv](https://docs.astral.sh/uv/)
- herdr on the same machine, speaking the protocol the app pins
- Tailscale, with MagicDNS and HTTPS certificates enabled for the tailnet

## Running it

```sh
uv sync
export HP_TAILNET_LOGIN=you@example.com
export HP_PUSH_SUBJECT=mailto:you@example.com   # Apple drops pushes without a real contact
uv run herdr-pwa serve                          # listens on 127.0.0.1:8772
uv run herdr-pwa pair                           # publishes it with tailscale serve, prints a QR
```

Open the URL on the phone, add it to the Home Screen, open it from there, and turn
alerts on. `uv run herdr-pwa doctor` checks the protocol, the push setup, the
harnesses the service can start and whether the running service is stale; it exits
non-zero when something is wrong.

Settings are `HP_*` environment variables (`src/herdr_pwa/settings.py`); state lives in
`~/.config/herdr-pwa-v2` unless `HP_STATE_DIR` says otherwise.

## Development

Run `pre-commit install` once per clone. `just check` runs every check (ruff,
basedpyright in strict mode, complexipy, pytest, the JS tests, the CSS token check and
a headless-Firefox geometry gate, so Node and Firefox are needed too), and CI runs the
same recipe.

Tests never touch a real herdr session: they run against captured output in
`tests/fixtures/` (see its README). `just live-session` starts a throwaway herdr session
for driving the app by hand. `docs/design.md` covers how the phone screens are built.
