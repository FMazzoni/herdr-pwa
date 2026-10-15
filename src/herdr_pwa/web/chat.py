"""Transcript turns turned into what the Chat tab renders: speaker, text, prose tree."""

from dataclasses import dataclass
from typing import Final

from herdr_pwa.engine.transcript import HistoryState, Speaker, TranscriptPage, TranscriptTurn
from herdr_pwa.web.markdown import MdNode, render_tree
from herdr_pwa.web.stats import duration

_STATE_NOTES = {
    HistoryState.empty: "Nothing has been said yet.",
    HistoryState.absent: "No transcript was found for this session yet.",
    HistoryState.unreadable: "The transcript exists and could not be read.",
    HistoryState.unsupported: "This harness keeps no transcript this app can read; see Pane.",
}


@dataclass(frozen=True, slots=True)
class ChatTurn:
    """One rendered line of conversation; only agent prose and peer messages become a tree."""

    speaker: str
    text: str
    prose: tuple[MdNode, ...]
    tool: str
    result: str
    failed: bool
    truncated: bool
    # Long enough to fold behind "Show all", so one wall of text does not bury the rest.
    long: bool = False
    # A tool call's id, which names its line across the polls that redraw it.
    call: str = ""
    # The record's own id, which names a message across redraws and an earlier page landing above.
    key: str = ""
    # A task notice's status word and how long it ran ("4m"), empty when unrecorded.
    status: str = ""
    elapsed: str = ""
    # A peer message's sender, and whether it is a subagent's hand-back report.
    sender: str = ""
    report: bool = False


@dataclass(frozen=True, slots=True)
class ToolRun:
    """Tool calls in a row, drawn as one quiet line; more than one fold behind "N tool calls"."""

    tools: tuple[ChatTurn, ...]

    @property
    def key(self) -> str:
        """Names the run by its first call, which stays first as the run grows."""
        first = self.tools[0]
        return first.call or f"{first.tool} {first.text}"[:120]

    @property
    def folded(self) -> bool:
        """Whether the run hides behind one "N tool calls" line."""
        return len(self.tools) > 1


# About eight lines at 390px (some forty characters each), or the text's own line breaks.
LONG_CHARS: Final = 320
LONG_LINES: Final = 8


def is_long(text: str) -> bool:
    """Whether a turn's text runs past what the Chat tab shows before "Show all"."""
    return len(text) > LONG_CHARS or text.count("\n") >= LONG_LINES


@dataclass(frozen=True, slots=True)
class ChatView:
    """A page of turns with tool calls gathered into runs, or the sentence that says why none."""

    items: tuple[ChatTurn | ToolRun, ...]
    note: str
    # The frontier this view starts at, the file it is in, and whether anything is older.
    start: int = 0
    log: str = ""
    more: bool = False


def fold_tools(turns: tuple[ChatTurn, ...]) -> tuple[ChatTurn | ToolRun, ...]:
    """Gather each row of tool calls into one run; a failed call is always a run of its own."""
    items: list[ChatTurn | ToolRun] = []
    run: list[ChatTurn] = []
    for turn in turns:
        if turn.speaker == Speaker.tool.value and not turn.failed:
            run.append(turn)
            continue
        if run:
            items.append(ToolRun(tuple(run)))
            run = []
        # Never folded away: what failed is shown where it happened.
        items.append(ToolRun((turn,)) if turn.speaker == Speaker.tool.value else turn)
    if run:
        items.append(ToolRun(tuple(run)))
    return tuple(items)


_PROSE: Final = (Speaker.agent, Speaker.peer)
_ONE_LINE: Final = (Speaker.tool, Speaker.notice)


def _turn(turn: TranscriptTurn) -> ChatTurn:
    prose = render_tree(turn.text) if turn.speaker in _PROSE else ()
    return ChatTurn(
        speaker=turn.speaker.value,
        text=turn.text,
        prose=prose,
        tool=turn.tool,
        result=turn.result,
        failed=turn.failed,
        truncated=turn.truncated or turn.result_truncated,
        long=turn.speaker not in _ONE_LINE and is_long(turn.text),
        call=turn.call,
        key=turn.uuid,
        status=turn.status,
        elapsed=duration(turn.elapsed_ms / 1000) if turn.elapsed_ms else "",
        sender=turn.sender,
        report=turn.report,
    )


def chat_view(page: TranscriptPage) -> ChatView:
    """The Chat tab's content for one transcript page."""
    turns = tuple(_turn(t) for t in page.turns)
    note = "" if turns else _STATE_NOTES.get(page.state, "Nothing has been said yet.")
    return ChatView(fold_tools(turns), note, start=page.cursor, log=page.log, more=page.has_more)
