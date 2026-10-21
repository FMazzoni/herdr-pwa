"""The herdr vocabulary: every name this app borrows from the runner it wraps."""

from typing import Final

DEFAULT_SOCKET_PATH: Final = "~/.config/herdr/herdr.sock"
EXPECTED_PROTOCOL: Final = 22

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
