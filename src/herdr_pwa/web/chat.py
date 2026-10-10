"""Transcript turns turned into what the Chat tab renders: speaker, text, prose tree."""

from dataclasses import dataclass
from typing import Final

from herdr_pwa.engine.transcript import HistoryState, Speaker, TranscriptPage, TranscriptTurn
from herdr_pwa.web.markdown import MdNode, render_tree

_STATE_NOTES = {
    HistoryState.empty: "Nothing has been said yet.",
    HistoryState.absent: "No transcript was found for this session yet.",
    HistoryState.unreadable: "The transcript exists and could not be read.",
    HistoryState.unsupported: "This harness keeps no transcript this app can read; see Pane.",
}


@dataclass(frozen=True, slots=True)
class ChatTurn:
    """One rendered line of conversation; only agent prose becomes a tree."""

    speaker: str
    text: str
    prose: tuple[MdNode, ...]
    tool: str
    result: str
    failed: bool
    truncated: bool
    # Long enough to fold behind "Show all", so one wall of text does not bury the rest.
    long: bool = False


# About eight lines at 390px (some forty characters each), or the text's own line breaks.
LONG_CHARS: Final = 320
LONG_LINES: Final = 8


def is_long(text: str) -> bool:
    """Whether a turn's text runs past what the Chat tab shows before "Show all"."""
    return len(text) > LONG_CHARS or text.count("\n") >= LONG_LINES


@dataclass(frozen=True, slots=True)
class ChatView:
    """A page of turns, or the one sentence that says why there are none."""

    turns: tuple[ChatTurn, ...]
    note: str


def _turn(turn: TranscriptTurn) -> ChatTurn:
    prose = render_tree(turn.text) if turn.speaker is Speaker.agent else ()
    return ChatTurn(
        speaker=turn.speaker.value,
        text=turn.text,
        prose=prose,
        tool=turn.tool,
        result=turn.result,
        failed=turn.failed,
        truncated=turn.truncated or turn.result_truncated,
        long=turn.speaker is not Speaker.tool and is_long(turn.text),
    )


def chat_view(page: TranscriptPage) -> ChatView:
    """The Chat tab's content for one transcript page."""
    turns = tuple(_turn(t) for t in page.turns)
    if turns:
        return ChatView(turns, "")
    return ChatView((), _STATE_NOTES.get(page.state, "Nothing has been said yet."))
