"""The herdr vocabulary: every name this app borrows from the runner it wraps."""

from typing import Final

BINARY: Final = "herdr"
DEFAULT_SOCKET_PATH: Final = "~/.config/herdr/herdr.sock"
SOCKET_ENV_VAR: Final = "HERDR_SOCKET_PATH"
EXPECTED_PROTOCOL: Final = 22

# The CLI spellings; nothing derives the socket method names from these.
CMD_SNAPSHOT: Final = ("api", "snapshot")
CMD_AGENT_GET: Final = ("agent", "get")
CMD_AGENT_READ: Final = ("agent", "read")
CMD_AGENT_PROMPT: Final = ("agent", "prompt")
CMD_AGENT_SEND_KEYS: Final = ("agent", "send-keys")
CMD_AGENT_FOCUS: Final = ("agent", "focus")
WRITE_COMMANDS: Final = frozenset({CMD_AGENT_PROMPT, CMD_AGENT_SEND_KEYS, CMD_AGENT_FOCUS})

FLAG_LINES: Final = "--lines"
FLAG_SOURCE: Final = "--source"
FLAG_FORMAT: Final = "--format"
FORMAT_TEXT: Final = "text"
FORMAT_ANSI: Final = "ansi"

# Socket methods that drive an agent; codegen owns the method names, this owns which ones write.
WRITE_METHODS: Final = frozenset(
    {
        "agent.prompt",
        "agent.send_keys",
        "agent.focus",
        "agent.start",
        "tab.create",
        "pane.close",
        "workspace.create",
        "workspace.rename",
        "workspace.close",
    }
)
