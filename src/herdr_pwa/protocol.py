"""The herdr vocabulary: every name this app borrows from the runner it wraps."""

from typing import Final


# ---------------------------------------------------------------------------
# How to reach the runner
# ---------------------------------------------------------------------------

BINARY: Final = "herdr"

DEFAULT_SOCKET_PATH: Final = "~/.config/herdr/herdr.sock"

SOCKET_ENV_VAR: Final = "HERDR_SOCKET_PATH"

EXPECTED_PROTOCOL: Final = 22

# ---------------------------------------------------------------------------
# Every subcommand this app may issue. Nothing outside this module builds one.
# ---------------------------------------------------------------------------

CMD_SNAPSHOT: Final = ("api", "snapshot")

CMD_AGENT_GET: Final = ("agent", "get")

CMD_AGENT_READ: Final = ("agent", "read")

CMD_AGENT_PROMPT: Final = ("agent", "prompt")

CMD_AGENT_SEND_KEYS: Final = ("agent", "send-keys")

CMD_AGENT_FOCUS: Final = ("agent", "focus")

WRITE_COMMANDS: Final = frozenset({CMD_AGENT_PROMPT, CMD_AGENT_SEND_KEYS, CMD_AGENT_FOCUS})

# Flags. `agent read` is the only subcommand this app passes options to.

FLAG_LINES: Final = "--lines"

FLAG_SOURCE: Final = "--source"

FLAG_FORMAT: Final = "--format"

FORMAT_TEXT: Final = "text"

FORMAT_ANSI: Final = "ansi"

# The socket vocabulary lives in the generated models, not here: codegen owns
# the ~90 dotted method names, and this file owns only what codegen cannot say.

WRITE_METHODS: Final = frozenset(
    {
        "agent.prompt",
        "agent.send_keys",
        "agent.focus",
        "agent.start",
        "tab.create",
        "pane.close",
        # A workspace close is the biggest write this app can make --
        # it ends every agent in the workspace at once -- so it is the last
        # method that may sit outside the guard this set arms.
        "workspace.create",
        "workspace.rename",
        "workspace.close",
    }
)
