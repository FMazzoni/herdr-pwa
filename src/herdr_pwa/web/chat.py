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
    """One rendered line of conversation; agent prose, peer messages and reports become a tree."""

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
    # How a task notice's task ended ("finished", "exit 0") and how long it ran ("4m").
    status: str = ""
    elapsed: str = ""
    # A command's ending in the harness's words, and the file its output went to.
    outcome: str = ""
    output: str = ""
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


def _prose(turn: TranscriptTurn) -> tuple[MdNode, ...]:
    if turn.speaker in _PROSE:
        return render_tree(turn.text)
    # An agent's notice opens on its report, which is prose like any other.
    return render_tree(turn.result) if _is_agent_notice(turn) else ()


def _is_agent_notice(turn: TranscriptTurn) -> bool:
    return turn.speaker is Speaker.notice and turn.tool == "Agent"


def _is_long(turn: TranscriptTurn) -> bool:
    if _is_agent_notice(turn):
        return is_long(turn.result)
    return turn.speaker not in _ONE_LINE and is_long(turn.text)


def _turn(turn: TranscriptTurn) -> ChatTurn:
    prose = _prose(turn)
    return ChatTurn(
        speaker=turn.speaker.value,
        text=turn.text,
        prose=prose,
        tool=turn.tool,
        result=turn.result,
        failed=turn.failed,
        truncated=turn.truncated or turn.result_truncated,
        long=_is_long(turn),
        call=turn.call,
        key=turn.uuid,
        status=turn.status,
        elapsed=duration(turn.elapsed_ms / 1000) if turn.elapsed_ms else "",
        sender=turn.sender,
        report=turn.report,
        outcome=turn.outcome,
        output=turn.output,
    )


# Between two reports one task sent, in the one prose tree they share.
_BETWEEN_REPORTS: Final = "\n\n---\n\n"


def _is_report(turn: TranscriptTurn) -> bool:
    return turn.speaker is Speaker.peer and turn.report and bool(turn.task)


def _notices_by_task(turns: tuple[TranscriptTurn, ...]) -> dict[str, list[int]]:
    places: dict[str, list[int]] = {}
    for i, turn in enumerate(turns):
        if _is_agent_notice(turn) and turn.task:
            places.setdefault(turn.task, []).append(i)
    return places


def _nearest(places: list[int], at: int) -> int:
    """The place in *places* nearest before *at*, else the first after it."""
    before = [p for p in places if p < at]
    return before[-1] if before else places[0]


def _hosts(turns: tuple[TranscriptTurn, ...]) -> dict[int, int]:
    """Each hand-back's place mapped to the place of the agent notice that will carry it.

    A report joins its task's notice nearest before it, else the first after it: a task that
    stops twice reports after the notice that says it has not yet, and once before the next.
    """
    notices = _notices_by_task(turns)
    return {
        i: _nearest(notices[turn.task], i)
        for i, turn in enumerate(turns)
        if _is_report(turn) and turn.task in notices
    }


def joined(turns: tuple[TranscriptTurn, ...]) -> tuple[TranscriptTurn, ...]:
    """Each hand-back moved into its task's notice, where it opens; its own card is not drawn.

    The harness writes a subagent's report as a message and says only "delivered" in the
    notice. Joined on the task id, the report is in one place, under the line that says which
    task it is. A report whose notice is not on the page stays a card, named by its type.
    """
    hosts = _hosts(turns)
    reports: dict[int, list[TranscriptTurn]] = {}
    for at, host in hosts.items():
        reports.setdefault(host, []).append(turns[at])
    return tuple(
        _carrying(turn, reports.get(i, [])) for i, turn in enumerate(turns) if i not in hosts
    )


def _carrying(notice: TranscriptTurn, reports: list[TranscriptTurn]) -> TranscriptTurn:
    """*notice* opening on *reports*, joined; any turn without reports, as it was."""
    if not reports:
        return notice
    text = _BETWEEN_REPORTS.join(r.text for r in reports)
    cut = any(r.truncated for r in reports)
    return notice.model_copy(update={"result": text, "result_truncated": cut})


def chat_view(page: TranscriptPage) -> ChatView:
    """The Chat tab's content for one transcript page."""
    turns = tuple(_turn(t) for t in joined(tuple(page.turns)))
    note = "" if turns else _STATE_NOTES.get(page.state, "Nothing has been said yet.")
    return ChatView(fold_tools(turns), note, start=page.cursor, log=page.log, more=page.has_more)
