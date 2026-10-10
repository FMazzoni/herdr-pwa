"""Transcript turns turned into what the Chat tab renders: speaker, text, prose tree."""

from dataclasses import dataclass

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
    )


def chat_view(page: TranscriptPage) -> ChatView:
    """The Chat tab's content for one transcript page."""
    turns = tuple(_turn(t) for t in page.turns)
    if turns:
        return ChatView(turns, "")
    return ChatView((), _STATE_NOTES.get(page.state, "Nothing has been said yet."))
