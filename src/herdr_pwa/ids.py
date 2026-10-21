"""The three ids herdr hands out, kept apart by type because each names a different thing."""

from typing import NewType

# An agent's identity, stable for its whole life.
SessionId = NewType("SessionId", str)
# Where an agent sits this tick; herdr recycles pane ids, so one never identifies an agent.
PaneId = NewType("PaneId", str)
# A workspace, the human-named group the herd is sorted by.
WorkspaceId = NewType("WorkspaceId", str)
