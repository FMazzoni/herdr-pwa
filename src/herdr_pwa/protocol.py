"""The herdr vocabulary: every name this app borrows from the runner it wraps."""

from typing import Final

__all__ = [
    "BINARY",
    "CMD_AGENT_FOCUS",
    "CMD_AGENT_GET",
    "CMD_AGENT_PROMPT",
    "CMD_AGENT_READ",
    "CMD_AGENT_SEND_KEYS",
    "CMD_SNAPSHOT",
    "DEFAULT_SOCKET_PATH",
    "EXPECTED_PROTOCOL",
    "FLAG_FORMAT",
    "FLAG_LINES",
    "FLAG_SOURCE",
    "FORMAT_ANSI",
    "FORMAT_TEXT",
    "SOCKET_ENV_VAR",
    "WRITE_COMMANDS",
    "WRITE_METHODS",
]

# ---------------------------------------------------------------------------
# How to reach the runner
# ---------------------------------------------------------------------------

BINARY: Final = "herdr"
"""The runner executable. The single string an upstream rename has to touch."""

DEFAULT_SOCKET_PATH: Final = "~/.config/herdr/herdr.sock"
"""Where the runner keeps its control socket when nothing says otherwise.
"""

SOCKET_ENV_VAR: Final = "HERDR_SOCKET_PATH"
"""The environment variable the runner itself honours for that socket.
"""

EXPECTED_PROTOCOL: Final = 22
"""Snapshot wire format this app is written against.
"""

# ---------------------------------------------------------------------------
# Every subcommand this app may issue. Nothing outside this module builds one.
# ---------------------------------------------------------------------------

CMD_SNAPSHOT: Final = ("api", "snapshot")
"""Read the whole session state."""

CMD_AGENT_GET: Final = ("agent", "get")
"""Read one agent by pane address."""

CMD_AGENT_READ: Final = ("agent", "read")
"""Read an agent's terminal text."""

CMD_AGENT_PROMPT: Final = ("agent", "prompt")
"""Submit a prompt to an agent. WRITE."""

CMD_AGENT_SEND_KEYS: Final = ("agent", "send-keys")
"""Send key presses to an agent. WRITE."""

CMD_AGENT_FOCUS: Final = ("agent", "focus")
"""Focus an agent's pane. WRITE."""

WRITE_COMMANDS: Final = frozenset(
    {CMD_AGENT_PROMPT, CMD_AGENT_SEND_KEYS, CMD_AGENT_FOCUS}
)
"""Subcommands that *drive* an agent.
"""

# Flags. `agent read` is the only subcommand this app passes options to.
# @doc: docs/modules/protocol.md

FLAG_LINES: Final = "--lines"
"""How many lines of terminal text to return."""

FLAG_SOURCE: Final = "--source"
"""Which snapshot source: visible | recent | recent-unwrapped | detection.
"""

FLAG_FORMAT: Final = "--format"
"""text | ansi."""

FORMAT_TEXT: Final = "text"
"""Plain text, with every escape sequence already removed by the runner.
"""

FORMAT_ANSI: Final = "ansi"
"""Escape sequences kept. Asked for by the drill-in mirror and by nothing else.
"""

# The socket vocabulary lives in the generated models, not here: codegen owns
# the ~90 dotted method names, and this file owns only what codegen cannot say.
# @doc: docs/modules/protocol.md

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
"""Socket methods that drive an agent, the twin of `WRITE_COMMANDS`.
"""
