"""Transcript turns turned into what the Chat tab renders: speaker, text, prose tree."""

import zlib
from dataclasses import dataclass
from typing import Final

from herdr_pwa.clock import duration, duration_said, elapsed
from herdr_pwa.decisions import QUIET_AFTER, gone_quiet, still_talking
from herdr_pwa.engine import (
    AgentStatus,
    HistoryState,
    Speaker,
    TranscriptPage,
    TranscriptTurn,
    is_api_error,
    is_compact_summary,
    is_slash_command,
)
from herdr_pwa.web.markdown import MdNode, render_tree
from herdr_pwa.web.stats import NO_TIMES, TurnTimes

_STATE_NOTES = {
    HistoryState.empty: "Nothing has been said yet.",
    HistoryState.absent: "No transcript was found for this session yet.",
    HistoryState.unreadable: "The transcript exists and could not be read.",
    HistoryState.unsupported: "This harness keeps no transcript this app can read; see Pane.",
}


# The turn's one word while the agent thinks between tools: decoration, which VoiceOver skips.
WORKING_VERBS: Final = (
    "Sketching",
    "Untangling",
    "Whittling",
    "Sifting",
    "Knitting",
    "Rummaging",
    "Weighing",
    "Tidying",
    "Gathering",
    "Folding",
    "Charting",
    "Polishing",
)

# The status bubble's looks, by name: a palette is one of app.css's [data-working-palette] blocks,
# a motion one entry of app.js's MOTIONS table. Changing either default is this one line.
WORKING_PALETTE: Final = "softer-ember"
WORKING_MOTION: Final = "crest"


def turn_verb(stretch: str) -> str:
    """The verb the working stretch named *stretch* wears: the same on every poll, and every run."""
    # crc32, not hash(): str hashes are salted per process, so a restart would change the verb.
    return WORKING_VERBS[zlib.crc32(stretch.encode()) % len(WORKING_VERBS)]


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
    # Under the newest turn: what the agent is doing while the turn is its own, or once the turn
    # is over, how long it worked.
    bubble: "Bubble | None" = None
    receipt: "Receipt | None" = None
    # Why the session the pane reports may not be the agent there; shown above everything.
    doubt: str = ""
    # The log could not be read under a drawing the reader already has, which then stands.
    held: bool = False


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
        api_error=is_api_error(turn),
        times=turn.times,
        slash=is_slash_command(turn),
        compacted=is_compact_summary(turn),
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


@dataclass(frozen=True, slots=True)
class Turning:
    """What the router knows of the turn beside its log: the agent's status, the stretch it is
    working in (herdr's sequence), what its card says it is asking, and the turn's clock."""

    status: AgentStatus = AgentStatus.UNKNOWN
    stretch: str = ""
    asked: str = ""
    times: TurnTimes = NO_TIMES


# An agent whose status is not known: no bubble, and no receipt without a clock.
NOT_TURNING: Final = Turning()


@dataclass(frozen=True, slots=True)
class Bubble:
    """The status bubble: its state, its words, what VoiceOver hears, and its clocks."""

    state: str
    text: str
    # Said when the state changes; the bubble's own words are for reading, not for announcing.
    said: str
    describe: str
    stretch: str
    # Whole seconds the turn has run, worded for the timer; None and "" draw no timer.
    ran: int | None = None
    time: str = ""
    # Whole seconds the log has been still, and how long it may be before the bubble says so.
    quiet: int | None = None
    quiet_after: int = QUIET_AFTER
    palette: str = WORKING_PALETTE
    motion: str = WORKING_MOTION


@dataclass(frozen=True, slots=True)
class Receipt:
    """The quiet line a finished turn folds into, and the same in words VoiceOver says well."""

    text: str
    said: str


# What a tool does, by its name in any harness, case aside; a tool not listed is "Using" itself.
_DOING: Final = {
    "bash": "Running",
    "shell": "Running",
    "terminal": "Running",
    "read": "Reading",
    "read_file": "Reading",
    "view": "Reading",
    "edit": "Editing",
    "multiedit": "Editing",
    "notebookedit": "Editing",
    "patch": "Editing",
    "apply_patch": "Editing",
    "write": "Writing",
    "write_file": "Writing",
    "grep": "Searching",
    "search_files": "Searching",
    "glob": "Finding",
    "webfetch": "Fetching",
    "websearch": "Searching",
    "task": "Delegating",
    "agent": "Delegating",
}
_FILE_TOOLS: Final = frozenset({"Reading", "Editing", "Writing"})
# What a command line opens with before the program it runs: a change of directory, a setting,
# or a wrapper that runs the next word; each is skipped so "Running" names the program.
_WRAPPERS: Final = frozenset(
    {"sudo", "env", "nice", "time", "timeout", "nohup", "setsid", "exec", "uv", "run", "npx"}
)


def _skipped(word: str) -> bool:
    return word in _WRAPPERS or word.startswith("-") or word.isdigit() or "=" in word


def _program(command: str) -> str:
    """The command line's first line from the program it runs, past cd, settings and wrappers."""
    first = command.strip().splitlines()[0] if command.strip() else ""
    parts = [part.strip() for part in first.split("&&")]
    line = next((part for part in parts if not part.startswith("cd ")), first)
    words = line.split()
    at = next((i for i, word in enumerate(words) if not _skipped(word)), 0)
    return " ".join(words[at:])


def doing(turn: ChatTurn) -> str:
    """A tool call as what it is doing to what: "Running pytest", "Editing chat.py"."""
    verb = _DOING.get(turn.tool.casefold())
    if verb is None:
        return f"Using {turn.tool}"
    if verb == "Running":
        return f"{verb} {_program(turn.text)}".strip()
    what = turn.text.rstrip("/").rsplit("/", 1)[-1] if verb in _FILE_TOOLS else turn.text
    return f"{verb} {' '.join(what.split())}".strip()


def _running(items: tuple[ChatTurn | ToolRun, ...]) -> ChatTurn | None:
    """The newest call when it is the newest thing in the log and has no result yet."""
    last = items[-1] if items else None
    call = last.tools[-1] if isinstance(last, ToolRun) else None
    return call if call is not None and not call.result and not call.failed else None


def _whole(seconds: float | None) -> int | None:
    return None if seconds is None else int(seconds)


def _working(items: tuple[ChatTurn | ToolRun, ...], turning: Turning) -> Bubble:
    times = turning.times
    ran = _whole(times.ran)
    time = elapsed(ran) if ran is not None else ""
    if gone_quiet(times.quiet):
        quiet = int(times.quiet or 0)
        text = f"No activity for {elapsed(quiet // 60 * 60)}"
        said = f"No activity for {duration_said(quiet)}"
        return Bubble("quiet", text, said, said, turning.stretch, quiet=quiet, ran=ran, time=time)
    call = _running(items)
    text = doing(call) if call is not None else f"{turn_verb(turning.stretch)}\u2026"
    describe = f"Working: {text}" if call is not None else "Working"
    quiet = _whole(times.quiet)
    return Bubble(
        "working", text, "Working", describe, turning.stretch, quiet=quiet, ran=ran, time=time
    )


def _blocked(turning: Turning) -> Bubble:
    asked = " ".join(turning.asked.split())
    text = f"Needs you \u00b7 {asked}" if asked else "Needs you"
    said = f"Needs you: {asked}" if asked else "Needs you"
    return Bubble("blocked", text, said, said, turning.stretch)


def _calls_since_prompt(items: tuple[ChatTurn | ToolRun, ...]) -> int | None:
    """Tool calls after the newest prompt on the page; None when no prompt is on it."""
    asked = [i for i, item in enumerate(items) if isinstance(item, ChatTurn) and _asks(item)]
    if not asked:
        return None
    return sum(len(item.tools) for item in items[asked[-1] :] if isinstance(item, ToolRun))


def _asks(item: ChatTurn) -> bool:
    return item.speaker == Speaker.human.value


def _receipt(items: tuple[ChatTurn | ToolRun, ...], took: float | None) -> Receipt | None:
    """How long the newest turn worked, once the agent has answered it."""
    last = items[-1] if items else None
    if took is None or last is None or (isinstance(last, ChatTurn) and _asks(last)):
        return None
    calls = _calls_since_prompt(items)
    counted = f"{calls} tool call{'' if calls == 1 else 's'}" if calls else ""
    text = " \u00b7 ".join(part for part in (f"Worked {elapsed(took)}", counted) if part)
    said = ", ".join(part for part in (f"Worked {duration_said(took)}", counted) if part)
    return Receipt(text, said)


def _status(
    items: tuple[ChatTurn | ToolRun, ...], turning: Turning
) -> tuple[Bubble | None, Receipt | None]:
    if still_talking(turning.status):
        return _working(items, turning), None
    if turning.status is AgentStatus.BLOCKED:
        return _blocked(turning), None
    return None, _receipt(items, turning.times.took)


def chat_view(
    page: TranscriptPage, *, turning: Turning = NOT_TURNING, doubt: str = "", drawn: bool = False
) -> ChatView:
    """The Chat tab's content for one transcript page.

    *turning* is what the agent's status and its harness's tally say about the turn: turns
    land at message boundaries, so while the agent works whatever it is saying now is not in
    the log yet, and a status bubble under the newest turn says what it is doing instead of
    the log looking complete. Once the turn is over the bubble folds into how long it worked.

    *drawn* says the reader already has turns on screen. A store its harness has
    locked reads as unreadable with no turns, and redrawn that would blank the log
    for a poll and lose the reader's place, so the view is ``held`` instead.
    """
    turns = tuple(_turn(t) for t in joined(tuple(page.turns)))
    note = "" if turns else _STATE_NOTES.get(page.state, "Nothing has been said yet.")
    held = drawn and not turns and page.state is HistoryState.unreadable
    items = fold_tools(turns)
    bubble, receipt = _status(items, turning)
    return ChatView(
        items,
        note,
        start=page.cursor,
        log=page.log,
        more=page.has_more,
        bubble=bubble,
        receipt=receipt,
        doubt=doubt,
        held=held,
    )
