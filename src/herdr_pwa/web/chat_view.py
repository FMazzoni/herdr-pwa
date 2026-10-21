"""The Chat tab's content: the transcript's turns, the agent's status under them, the row."""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

from herdr_pwa.engine import HistoryState, TranscriptPage
from herdr_pwa.web.chat import ChatTurn, ToolRun, chat_turn, fold_tools, joined
from herdr_pwa.web.chat_status import (
    NOT_TURNING,
    AgentLine,
    Bubble,
    BubbleState,
    Receipt,
    Turning,
    lines,
    status,
)

_STATE_NOTES: Final = MappingProxyType(
    {
        HistoryState.empty: "Nothing has been said yet.",
        HistoryState.absent: "No transcript was found for this session yet.",
        HistoryState.unreadable: "The transcript exists and could not be read.",
        HistoryState.unsupported: "This harness keeps no transcript this app can read.",
    }
)
# What it is doing can then be read only on its Pane, so the note offers the way there.
_READ_ON_PANE: Final = frozenset({HistoryState.unsupported})

# The status row's state: the bubble's, else background work, a finished turn, or nothing.
type RowState = BubbleState | Literal["background", "done", "still"]


@dataclass(frozen=True, slots=True)
class ChatView:
    """A page of turns with tool calls gathered into runs, or the sentence that says why none."""

    items: tuple[ChatTurn | ToolRun, ...]
    note: str
    # The frontier this view starts at, the file it is in, and whether anything is older.
    start: int = 0
    log: str = ""
    more: bool = False
    # Under the newest turn: what the agent is doing, or once the turn is over, how long it worked.
    bubble: Bubble | None = None
    receipt: Receipt | None = None
    # Under the bubble: its background agents, running first as its pane lists them.
    agents: tuple[AgentLine, ...] = ()
    shells: tuple[AgentLine, ...] = ()
    # Why the session the pane reports may not be the agent there; shown above everything.
    doubt: str = ""
    # The log could not be read under a drawing the reader already has, which then stands.
    held: bool = False
    # The note sends the reader to the Pane tab, the only place this agent can be read.
    door: bool = False
    # What the person sent that waits in the harness's queue, not yet in the log as said.
    queued: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Row:
    """The status row above the composer: its state, what still runs, and what waits."""

    state: RowState
    running: int
    queued: int
    lead: str
    sentence: str
    shells: int = 0


def chat_view(
    page: TranscriptPage, *, turning: Turning = NOT_TURNING, doubt: str = "", drawn: bool = False
) -> ChatView:
    """The Chat tab's content for one transcript page.

    Args:
        page: The transcript page read.
        turning: What status and tally say of the turn, for the bubble the log cannot show yet.
        doubt: Why the page may be incomplete, said above it.
        drawn: The reader already has turns, so a locked store holds the view, not blanks it.
    """
    turns = tuple(chat_turn(t) for t in joined(tuple(page.turns)))
    note = "" if turns else _STATE_NOTES.get(page.state, "Nothing has been said yet.")
    held = drawn and not turns and page.state is HistoryState.unreadable
    items = fold_tools(turns)
    bubble, receipt = status(items, turning)
    # Beside a live turn, or while something it started runs; the router drops a dead process's.
    listed = bubble is not None or any(agent.running for agent in turning.spawned)
    spawned = turning.spawned if listed else ()
    return ChatView(
        items,
        note,
        start=page.cursor,
        log=page.log,
        more=page.has_more,
        bubble=bubble,
        receipt=receipt,
        agents=lines(spawned, shells=False),
        shells=lines(spawned, shells=True),
        doubt=doubt,
        held=held,
        door=not turns and page.state in _READ_ON_PANE,
        queued=tuple(page.queued),
    )


def status_row(view: ChatView, word: str) -> Row:
    """The row for *view*: its bubble's words, else the finished turn's, else *word*, the status."""
    running = sum(1 for agent in view.agents if agent.state == "running")
    shells = sum(1 for shell in view.shells if shell.state == "running")
    queued = len(view.queued)
    state: RowState
    if view.bubble is not None:
        state, lead = view.bubble.state, view.bubble.describe
    elif running or shells:
        state, lead = "background", word.capitalize()
    elif view.receipt is not None:
        state, lead = "done", view.receipt.said
    else:
        state, lead = "still", word.capitalize()
    counts = (
        _counted(running, "agent", "agents", "running"),
        _counted(shells, "shell", "shells", "running"),
        _counted(queued, "message", "messages", "queued"),
    )
    sentence = "".join(f"{part}. " for part in (lead, *counts) if part).strip()
    return Row(state, running, queued, lead, sentence, shells)


def _counted(n: int, one: str, many: str, tail: str) -> str:
    return f"{n} {one if n == 1 else many} {tail}" if n else ""
