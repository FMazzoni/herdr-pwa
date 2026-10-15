# One command, non-zero on any problem. CI runs exactly this recipe.

default: check

check:
    uv run ruff format --check .
    uv run ruff check .
    uv run basedpyright
    uv run complexipy --quiet src
    uv run pytest -q
    node --test tests/js/*.test.mjs
    uv run python scripts/css_tokens.py
    uv run python scripts/geometry_gate.py

# Clone or fetch the foreign capture repos at the ledger's pinned commits into $HP_FOREIGN_CAPTURES
# (default ~/.cache/herdr-pwa/foreign-captures), shared by every worktree; the ledger fails without
# them. Never vendored: one of them is AGPL.
corpus:
    uv run python scripts/corpus.py

# Headless Firefox over the real templates at 390px, 430px and 320px: no sideways scroll, 44px tap
# targets, content inside the viewport. Skips when a receipt for this exact tree exists in
# ~/.cache/herdr-pwa-v2/geometry, which every worktree shares.
geometry:
    uv run python scripts/geometry_gate.py

# The same checks with no receipt consulted, for when the fingerprint is not trusted.
geometry-force:
    uv run python scripts/geometry_gate.py --force

# WebKit as an iPhone (390x844 at 3x, touch, Safari's UA), at 430x932 and at 320px: a first-viewport
# and a full-page PNG per catalogue state under .agents/shots/. For eyes, not a gate; skips without
# WebKit.
shots *KEYS:
    uv run python scripts/shots.py {{KEYS}}

# The old app | this one, per state key, as an HTML contact sheet under .agents/sheets/.
# --embed --out PATH writes one self-contained file; --left DIR compares against saved shots.
sheet *ARGS:
    uv run python scripts/sheet.py {{ARGS}}

# The browser the shots use; a dev tool, fetched into ~/.cache/ms-playwright, never committed.
webkit:
    uv run playwright install webkit

# Print the latest phone device report from the state dir (HP_STATE_DIR, default ~/.config/herdr-pwa-v2).
device-report *args:
    uv run herdr-pwa device-report {{args}}

# Serve on the build port against the throwaway hp-test session, never the default one.
live-serve: live-session
    HP_SOCKET_PATH="$HOME/.config/herdr/sessions/hp-test/herdr.sock" uv run herdr-pwa serve

# A disposable hp-test herdr session with its own server and socket; never the default one.
live-session:
    uv run python scripts/live_session.py up

# Start (or reuse) a cheap claude-on-haiku agent in the hp-test session.
live-agent: live-session
    uv run python scripts/live_session.py agent

# Show the hp-test socket and every agent in it.
live-status:
    uv run python scripts/live_session.py status

# Stop the hp-test session; its workspace returns on the next live-session.
live-down:
    uv run python scripts/live_session.py down
