"""What the agent is doing now, under the newest turn: the bubble, the receipt, its background work."""

import zlib
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

from herdr_pwa.clock import duration_said, elapsed
from herdr_pwa.decisions import QUIET_AFTER, gone_quiet, needs_you, still_talking
from herdr_pwa.engine import SPAWNED_SHELL, AgentStatus, Speaker
from herdr_pwa.web.chat import ChatTurn, ToolRun
from herdr_pwa.web.stats import NO_TIMES, SpawnedTime, TurnTimes

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

# The bubble's default looks: an app.css [data-working-palette] block and an hp.js MOTIONS entry.
WORKING_PALETTE: Final = "softer-ember"
WORKING_MOTION: Final = "crest"

# What a tool does, by its name in any harness, case aside; a tool not listed is "Using" itself.
_DOING: Final = MappingProxyType(
    {
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
)
_FILE_TOOLS: Final = frozenset({"Reading", "Editing", "Writing"})
# Prefixes before the program a command runs (cd, settings, wrappers), skipped to name the program.
_WRAPPERS: Final = frozenset(
    {"sudo", "env", "nice", "time", "timeout", "nohup", "setsid", "exec", "uv", "run", "npx"}
)
# A shell's name (its description or first command line) is cut here; the sheet ellipsizes the rest.
_SHELL_CHARS: Final = 120

# What the bubble shows: a turn at work, one gone quiet, or one waiting on the person.
type BubbleState = Literal["working", "quiet", "blocked"]
# Where one background agent or shell is.
type LineState = Literal["running", "failed", "finished"]


@dataclass(frozen=True, slots=True)
class Turning:
    """What the router knows of the turn beside its log: status, stretch, question and clock."""

    status: AgentStatus = AgentStatus.UNKNOWN
    stretch: str = ""
    asked: str = ""
    times: TurnTimes = NO_TIMES
    spawned: tuple[SpawnedTime, ...] = ()


# An agent whose status is not known: no bubble, and no receipt without a clock.
NOT_TURNING: Final = Turning()


@dataclass(frozen=True, slots=True)
class Bubble:
    """The status bubble: its state, its words, what VoiceOver hears, and its clocks."""

    state: BubbleState
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
class AgentLine:
    """One background agent under the bubble: what it was asked, where it is, and for how long."""

    description: str
    state: LineState
    time: str = ""


@dataclass(frozen=True, slots=True)
class Receipt:
    """The quiet line a finished turn folds into, and the same in words VoiceOver says well."""

    text: str
    said: str


def status(
    items: tuple[ChatTurn | ToolRun, ...], turning: Turning
) -> tuple[Bubble | None, Receipt | None]:
    """The bubble while the turn runs or waits on the person, else the receipt once it is over."""
    if still_talking(turning.status):
        return _working(items, turning), None
    if needs_you(turning.status):
        return _blocked(turning), None
    return None, _receipt(items, turning.times.took)


def lines(spawned: tuple[SpawnedTime, ...], *, shells: bool) -> tuple[AgentLine, ...]:
    """The lines for *spawned*'s agents, or with *shells* its shells, running first."""
    # Running first, and of those what the last turn started; stable, so each keeps its age order.
    ordered = sorted(spawned, key=lambda a: (not a.running, not a.fresh))
    return tuple(_agent_line(a) for a in ordered if (a.kind == SPAWNED_SHELL) == shells)


def doing(turn: ChatTurn) -> str:
    """A tool call as what it is doing to what: "Running pytest", "Editing chat.py"."""
    verb = _DOING.get(turn.tool.casefold())
    if verb is None:
        return f"Using {turn.tool}"
    if verb == "Running":
        return f"{verb} {_program(turn.text)}".strip()
    what = turn.text.rstrip("/").rsplit("/", 1)[-1] if verb in _FILE_TOOLS else turn.text
    return f"{verb} {' '.join(what.split())}".strip()


def quiet_words(seconds: int) -> str:
    """A quiet stretch as the row says it, to its largest unit: "Quiet 3m", "Quiet 9d"."""
    return f"Quiet {elapsed(seconds // 60 * 60).split(' ')[0]}"


def turn_verb(stretch: str) -> str:
    """The verb the working stretch named *stretch* wears: the same on every poll, and every run."""
    # crc32, not hash(): str hashes are salted per process, so a restart would change the verb.
    return WORKING_VERBS[zlib.crc32(stretch.encode()) % len(WORKING_VERBS)]


def _working(items: tuple[ChatTurn | ToolRun, ...], turning: Turning) -> Bubble:
    times = turning.times
    ran = _whole(times.ran)
    time = elapsed(ran) if ran is not None else ""
    call = _running(items)
    # An open call is work however long it is silent (a test run writes nothing till it ends).
    if call is not None:
        text = doing(call)
        return Bubble("working", text, "Working", f"Working: {text}", turning.stretch, ran, time)
    # Its own log is still while agents it started work, which is not the turn gone quiet.
    if _waiting_on(turning.spawned):
        return Bubble(
            "working", "Waiting", "Working", "Working: Waiting", turning.stretch, ran, time
        )
    if gone_quiet(times.quiet):
        quiet = int(times.quiet or 0)
        said = f"No activity for {duration_said(quiet)}"
        # One duration: the turn's own clock beside the quiet one said the same span twice.
        return Bubble("quiet", quiet_words(quiet), said, said, turning.stretch, ran, "", quiet)
    text = f"{turn_verb(turning.stretch)}…"
    quiet = _whole(times.quiet)
    return Bubble("working", text, "Working", "Working", turning.stretch, ran, time, quiet)


def _whole(seconds: float | None) -> int | None:
    return None if seconds is None else int(seconds)


def _running(items: tuple[ChatTurn | ToolRun, ...]) -> ChatTurn | None:
    """The newest call when it is the newest thing in the log and has no result yet."""
    last = items[-1] if items else None
    call = last.tools[-1] if isinstance(last, ToolRun) else None
    return call if call is not None and not call.result and not call.failed else None


def _waiting_on(spawned: tuple[SpawnedTime, ...]) -> bool:
    """Whether the turn waits on agents it started; a shell left running is not waited on."""
    return any(agent.running and agent.kind != SPAWNED_SHELL for agent in spawned)


def _blocked(turning: Turning) -> Bubble:
    asked = " ".join(turning.asked.split())
    said = f"Needs you: {asked}" if asked else "Needs you"
    # The question is drawn in full just above, so the row does not say it again.
    return Bubble("blocked", "Needs you", said, said, turning.stretch)


def _receipt(items: tuple[ChatTurn | ToolRun, ...], took: float | None) -> Receipt | None:
    """How long the newest turn worked, once the agent has answered it."""
    last = items[-1] if items else None
    if took is None or last is None or (isinstance(last, ChatTurn) and _asks(last)):
        return None
    calls = _calls_since_prompt(items)
    counted = f"{calls} tool call{'' if calls == 1 else 's'}" if calls else ""
    text = " · ".join(part for part in (f"Worked {elapsed(took)}", counted) if part)
    said = ", ".join(part for part in (f"Worked {duration_said(took)}", counted) if part)
    return Receipt(text, said)


def _asks(item: ChatTurn) -> bool:
    return item.speaker == Speaker.human.value


def _calls_since_prompt(items: tuple[ChatTurn | ToolRun, ...]) -> int | None:
    """Tool calls after the newest prompt on the page; None when no prompt is on it."""
    asked = [i for i, item in enumerate(items) if isinstance(item, ChatTurn) and _asks(item)]
    if not asked:
        return None
    return sum(len(item.tools) for item in items[asked[-1] :] if isinstance(item, ToolRun))


def _agent_line(agent: SpawnedTime) -> AgentLine:
    state: LineState = "running" if agent.running else "failed" if agent.failed else "finished"
    time = elapsed(agent.seconds) if agent.seconds is not None else ""
    if agent.kind == SPAWNED_SHELL:
        return AgentLine(agent.description[:_SHELL_CHARS] or "Shell", state, time)
    return AgentLine(agent.description or "Agent", state, time)


def _program(command: str) -> str:
    """The command line's first line from the program it runs, past cd, settings and wrappers."""
    first = command.strip().splitlines()[0] if command.strip() else ""
    parts = [part.strip() for part in first.split("&&")]
    line = next((part for part in parts if not part.startswith("cd ")), first)
    words = line.split()
    at = next((i for i, word in enumerate(words) if not _skipped(word)), 0)
    return " ".join(words[at:])


def _skipped(word: str) -> bool:
    return word in _WRAPPERS or word.startswith("-") or word.isdigit() or "=" in word
