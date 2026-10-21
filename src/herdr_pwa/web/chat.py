"""Transcript turns turned into what the Chat tab renders: speaker, text, prose tree."""

from dataclasses import dataclass
from typing import Final

from herdr_pwa.clock import duration
from herdr_pwa.engine import (
    Speaker,
    TranscriptTurn,
    is_api_error,
    is_compact_summary,
    is_slash_command,
)
from herdr_pwa.render.markdown import MdNode, render_tree

# About eight lines at 390px (some forty characters each), or the text's own line breaks.
LONG_CHARS: Final = 320
LONG_LINES: Final = 8

_PROSE: Final = (Speaker.agent, Speaker.peer)
_ONE_LINE: Final = (Speaker.tool, Speaker.notice)
# Between two reports one task sent, in the one prose tree they share.
_BETWEEN_REPORTS: Final = "\n\n---\n\n"


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
    # The harness's line that the API refused a turn, and how many identical refusals it stands for.
    api_error: bool = False
    times: int = 1
    # A slash command the person ran in the harness, opening on what it printed if anything.
    slash: bool = False
    # The harness's summary of what a compaction dropped, with no /compact line to open from.
    compacted: bool = False


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


def chat_turn(turn: TranscriptTurn) -> ChatTurn:
    """*turn* as the Chat tab draws it, its prose parsed and its length judged."""
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
        api_error=is_api_error(turn),
        times=turn.times,
        slash=is_slash_command(turn),
        compacted=is_compact_summary(turn),
    )


def joined(turns: tuple[TranscriptTurn, ...]) -> tuple[TranscriptTurn, ...]:
    """Each hand-back moved into its task's notice; one with no notice on the page stays a card."""
    hosts = _hosts(turns)
    reports: dict[int, list[TranscriptTurn]] = {}
    for at, host in hosts.items():
        reports.setdefault(host, []).append(turns[at])
    return tuple(
        _carrying(turn, reports.get(i, [])) for i, turn in enumerate(turns) if i not in hosts
    )


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


def is_long(text: str) -> bool:
    """Whether a turn's text runs past what the Chat tab shows before "Show all"."""
    return len(text) > LONG_CHARS or text.count("\n") >= LONG_LINES


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


def _hosts(turns: tuple[TranscriptTurn, ...]) -> dict[int, int]:
    """Each hand-back's place mapped to its task's nearest notice before it, else after it."""
    notices = _notices_by_task(turns)
    return {
        i: _nearest(notices[turn.task], i)
        for i, turn in enumerate(turns)
        if _is_report(turn) and turn.task in notices
    }


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


def _is_report(turn: TranscriptTurn) -> bool:
    return turn.speaker is Speaker.peer and turn.report and bool(turn.task)


def _carrying(notice: TranscriptTurn, reports: list[TranscriptTurn]) -> TranscriptTurn:
    """*notice* opening on *reports*, joined; any turn without reports, as it was."""
    if not reports:
        return notice
    text = _BETWEEN_REPORTS.join(r.text for r in reports)
    cut = any(r.truncated for r in reports)
    return notice.model_copy(update={"result": text, "result_truncated": cut})
