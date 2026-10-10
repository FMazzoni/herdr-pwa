# One command, non-zero on any problem. CI runs exactly this recipe.

default: check

check:
    uv run ruff format --check .
    uv run ruff check .
    uv run basedpyright
    uv run complexipy --quiet src
    uv run pytest -q

# Serve on the build port against the throwaway hp-test session, never the default one.
live-serve:
    HP_SOCKET_PATH="$HOME/.config/herdr/sessions/hp-test/herdr.sock" uv run herdr-pwa serve
