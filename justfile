# One command, non-zero on any problem. CI runs exactly this recipe.

default: check

check:
    uv run ruff format --check .
    uv run ruff check .
    uv run basedpyright
    uv run complexipy --quiet src
    uv run pytest -q

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
