"""What the dashboard decides about a screen and a herd, as typed records the views render."""

import posixpath
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from herdr_pwa.clock import ago
from herdr_pwa.engine import (
    CONTEXT,
    Agent,
    AgentStatus,
    BlockedPrompt,
    KeyHint,
    MenuShape,
    PaneId,
    SessionId,
    TranscriptTurn,
    WorkspaceId,
    ends_refused,
    rank,
)
from herdr_pwa.panetext import printed_whole

# Said, not drawn as a herdr state: the question went, and herdr has not yet said what replaced it.
UPDATING: Final = "updating"
IDLE_CADENCE: Final = 12
WORKING_CADENCE: Final = 5
# Seconds a working turn's log may stay still before the Chat says "No activity". Past a long
# thought or a slow test run, both of which write nothing until they end, and short enough that a
# glance within two minutes learns the agent may be stuck.
QUIET_AFTER: Final = 90
# Past this many seconds the herd is offline rather than merely late.
DISCONNECTED_AFTER: Final = 60
# A two-digit option needs two keystrokes, and the first one already answers row 1.
MAX_TAPPABLE_OPTION: Final = 9
# A row whose agent herdr reported outside any workspace.
_NO_WORKSPACE: Final = WorkspaceId("")
# Both measured on live screens: the digit addresses the row absolutely on either shape.
TAPPABLE_SHAPES: Final = frozenset({MenuShape.SINGLE_CHOICE, MenuShape.MULTI_SELECT})
# Once a row is its own button these only walked the cursor to it.
CURSOR_KEYS: Final = frozenset({"up", "down", "enter"})
SECTIONS: Final[tuple[tuple[AgentStatus, str], ...]] = (
    (AgentStatus.BLOCKED, "Needs you"),
    (AgentStatus.DONE, "Finished"),
    (AgentStatus.WORKING, "Working"),
    (AgentStatus.IDLE, "Idle"),
    (AgentStatus.UNKNOWN, "Unknown"),
)


@dataclass(frozen=True)
class OptionRow:
    """One numbered option, and the digit that answers it if it earned one."""

    number: int
    label: str
    checked: bool | None
    digit: str


@dataclass(frozen=True)
class StepCount:
    """How far through a multi-step question the screen is."""

    answered: int
    total: int


@dataclass(frozen=True)
class ScreenDecision:
    """Everything the answer surface decides about one blocked screen."""

    shape: MenuShape
    question: str
    options: tuple[OptionRow, ...]
    hints: tuple[KeyHint, ...]
    answer_hints: tuple[KeyHint, ...]
    handoffs: tuple[KeyHint, ...]
    steps: StepCount | None
    corroborated: bool
    drew_rows: bool
    asked: bool
    is_choice: bool
    focus: int | None

    @property
    def ticked(self) -> tuple[int, ...]:
        """Indices of the options whose box is ticked."""
        return tuple(i for i, option in enumerate(self.options) if option.checked)

    @property
    def checkable(self) -> int:
        """How many options carry a box at all."""
        return sum(1 for option in self.options if option.checked is not None)


class Condition(StrEnum):
    """Which honest state the feed's data is in."""

    LIVE = "live"
    STALE = "stale"
    DEGRADED = "degraded"
    OFFLINE = "offline"


@dataclass(frozen=True)
class Freshness:
    """How old the herd on screen is, and what the feed should say about it."""

    age: float | None
    age_label: str
    condition: Condition

    @property
    def stale(self) -> bool:
        """Every condition but live dims the feed: what is shown may not be now."""
        return self.condition is not Condition.LIVE


@dataclass(frozen=True, slots=True)
class Pending:
    """Work an idle agent started that is still to come back: agents that will report, and
    shells its last turn started."""

    agents: int = 0
    shells: int = 0

    def __bool__(self) -> bool:
        return bool(self.agents or self.shells)


NOTHING_PENDING: Final = Pending()


@dataclass(frozen=True)
class AgentRow:
    """One feed row: who the agent is, where it sits, and how to answer it."""

    session: SessionId
    target: PaneId
    title: str
    harness: str
    workspace: str
    status: AgentStatus
    effective_status: AgentStatus
    feed_key: tuple[int, int, int, str]
    answer: ScreenDecision | None
    workspace_id: WorkspaceId = _NO_WORKSPACE
    # Stopped on a full context: what it needs is said by the transcript, not asked on the screen.
    stalled: bool = False
    # Idle with work it started still to come back; such a card leads the idle ones.
    pending: Pending = NOTHING_PENDING


@dataclass(frozen=True)
class Section:
    """A run of feed rows sharing one effective status."""

    key: AgentStatus
    label: str
    rows: tuple[AgentRow, ...]

    @property
    def count(self) -> int:
        """How many rows the section holds."""
        return len(self.rows)


@dataclass(frozen=True)
class HerdDecision:
    """The whole feed over one snapshot: order, sections, tallies, polling, freshness."""

    rows: tuple[AgentRow, ...]
    sections: tuple[Section, ...]
    counts: Mapping[AgentStatus, int]
    cadence: int
    freshness: Freshness


def _options_refused(prompt: BlockedPrompt) -> bool:
    # A label the terminal cut short is not one the user can be shown as a whole answer.
    return any(not printed_whole(prompt.raw, option.label) for option in prompt.options)


def answer_digits(prompt: BlockedPrompt) -> tuple[str, ...]:
    """The digit each option row sends, or none when no row has earned one."""
    if not prompt.options or prompt.shape not in TAPPABLE_SHAPES:
        return ()
    # Measured live: beside a preview a digit only moves the cursor, so it is no answer.
    if prompt.digits_move or _options_refused(prompt):
        return ()
    numbers = [option.number for option in prompt.options]
    if not all(1 <= number <= MAX_TAPPABLE_OPTION for number in numbers):
        return ()
    return tuple(str(number) for number in numbers)


def rows_answer(prompt: BlockedPrompt) -> bool:
    """Whether tapping a row ends the question rather than changing what it holds."""
    # A wizard step: the digit picks the row, but more steps follow it.
    if prompt.stepper is not None:
        return False
    return bool(answer_digits(prompt)) and prompt.shape is MenuShape.SINGLE_CHOICE


def rows_toggle(prompt: BlockedPrompt) -> bool:
    """Whether tapping a row ticks a box the screen keeps, ending nothing."""
    return bool(answer_digits(prompt)) and prompt.shape is MenuShape.MULTI_SELECT


def answer_hints(prompt: BlockedPrompt, *, digits: tuple[str, ...]) -> tuple[KeyHint, ...]:
    """The footer keys still worth a button once the rows themselves are buttons."""
    # On a checklist Enter submits, so only a row that answers retires the cursor keys.
    if not digits or not rows_answer(prompt):
        return prompt.hints
    return tuple(h for h in prompt.hints if h.key not in CURSOR_KEYS)


def decide_screen(prompt: BlockedPrompt) -> ScreenDecision:
    """Every answer-surface decision about one parsed screen."""
    digits = answer_digits(prompt)
    stepper = prompt.stepper
    return ScreenDecision(
        shape=prompt.shape,
        question=prompt.question,
        options=tuple(
            OptionRow(
                number=option.number,
                label=option.label,
                checked=option.checked,
                digit=digits[i] if digits else "",
            )
            for i, option in enumerate(prompt.options)
        ),
        hints=prompt.hints,
        answer_hints=answer_hints(prompt, digits=digits),
        handoffs=prompt.handoffs,
        steps=None if stepper is None else StepCount(stepper.answered, stepper.total),
        corroborated=prompt.corroborated,
        drew_rows=prompt.drew_rows,
        asked=prompt.asked,
        is_choice=prompt.is_choice,
        focus=prompt.focus,
    )


def screen_asks(prompt: BlockedPrompt) -> bool:
    """Whether a screen herdr calls stopped still holds a dialog waiting on a person."""
    # Strict: recognised rows AND keys under them; a finished turn's numbered list has no keys.
    return prompt.is_choice and prompt.corroborated and prompt.answerable


def _within(inner: str, outer: str) -> bool:
    """Is *inner* the directory *outer* or one below it? Compared as paths, not as prefixes."""
    return inner == outer or inner.startswith(outer.rstrip("/") + "/")


def _related(one: str, other: str) -> bool:
    """Does either directory contain the other?"""
    return _within(one, other) or _within(other, one)


def identity_doubt(origin: str, cwds: Iterable[str]) -> str:
    """Why the session a pane reports may not be the agent running there, or ``""`` if no reason.

    *origin* is where the session's own store says it was started; *cwds* are where herdr says
    the pane runs. A session started somewhere unrelated to the pane was reported by something
    else that carries the pane's id, a nested harness for one. Nothing known is not a doubt.
    """
    started = posixpath.normpath(origin.strip()) if origin.strip() else ""
    here = [posixpath.normpath(cwd.strip()) for cwd in cwds if cwd.strip()]
    if not started or not here or any(_related(started, cwd) for cwd in here):
        return ""
    return (
        f"This pane reports a session that was started in {started}, but the pane runs in "
        f"{here[0]}. Something else in the pane may have reported its own session, so what "
        "is shown here may not be this agent's, and nothing is sent to it until the pane "
        "reports a session that started where it runs."
    )


@dataclass(frozen=True, slots=True)
class Overlay:
    """What the app read beside herdr's status: finishes the phone showed, and who stalled."""

    # None when nothing tracks what was read; then no finish is taken for read.
    unread: Mapping[str, bool] | None = None
    # Sessions whose conversation ends refused for a full context.
    stalled: frozenset[str] = frozenset()
    # Sessions herdr calls stopped whose screen still holds a dialog: its rules can miss one.
    asking: frozenset[str] = frozenset()
    # Idle sessions with work still to come back, read from their logs.
    pending: Mapping[str, Pending] = field(default_factory=dict[str, Pending])


NO_OVERLAY: Final = Overlay()
# herdr's word for a turn that has ended, and for the moment a retry runs before it is refused.
_STOPS: Final = frozenset({AgentStatus.IDLE, AgentStatus.DONE})
_STALLABLE: Final = _STOPS | {AgentStatus.WORKING}


def context_stalled(turns: Sequence[TranscriptTurn]) -> bool:
    """Whether the conversation ends refused for a full context, its woken retries looked past."""
    refused = ends_refused(turns)
    return refused is not None and refused.tool == CONTEXT


def reads_stall(status: AgentStatus, *, held: bool) -> bool:
    """Whether an agent's log is worth reading for a stall: at a stop, or retrying while stalled."""
    # A working agent's newest turn may not be written yet, so only a stall already held is kept.
    return status in _STOPS or (held and status is AgentStatus.WORKING)


def reads_dialog(status: AgentStatus) -> bool:
    """Whether a screen is worth reading for a question: herdr saw one, or says it stopped."""
    return status is AgentStatus.BLOCKED or hides_dialog(status)


def asks_now(status: AgentStatus, prompt: BlockedPrompt) -> bool:
    """Whether a screen just read holds the question: herdr saw it, or a stop's screen asks."""
    return status is AgentStatus.BLOCKED or (hides_dialog(status) and screen_asks(prompt))


def shows_question(agent: Agent, overlay: Overlay) -> bool:
    """Whether *agent*'s screen holds the question it waits on, as herdr or the app read it."""
    return agent.status is AgentStatus.BLOCKED or _unflagged(agent.status, agent.session, overlay)


def hides_dialog(status: AgentStatus) -> bool:
    """Whether herdr's word may hide a dialog its rules missed: only a stop is read for one."""
    return status in _STOPS


def _unflagged(status: AgentStatus, session: SessionId, overlay: Overlay) -> bool:
    return session in overlay.asking and hides_dialog(status)


def read_status(status: AgentStatus, session: SessionId, overlay: Overlay) -> AgentStatus:
    """The status every surface shows: a stall or a dialog needs you, a finish shown is idle."""
    # A full context asks for the person as surely as a question does, and outranks a finish.
    if session in overlay.stalled and status in _STALLABLE:
        return AgentStatus.BLOCKED
    if _unflagged(status, session, overlay):
        return AgentStatus.BLOCKED
    # herdr keeps done until the desk focuses the pane; reading it on the phone is our own overlay.
    unread = overlay.unread
    if status is not AgentStatus.DONE or unread is None:
        return status
    # A session missing from the seen ledger has never been read.
    return status if unread.get(session, True) else AgentStatus.IDLE


def effective_status(agent: Agent, overlay: Overlay = NO_OVERLAY) -> AgentStatus:
    """The status the feed acts on: a stall needs you, a finished agent already read is idle."""
    return read_status(agent.status, agent.session, overlay)


def page_status(agent: Agent, overlay: Overlay, *, answered: bool) -> str:
    """The agent page's status word: "updating" over a question just answered, else the feed's."""
    if answered and agent.status is AgentStatus.BLOCKED:
        return UPDATING
    return effective_status(agent, overlay).value


def still_talking(status: AgentStatus) -> bool:
    """Whether the agent is producing a turn its log has not received yet."""
    # Turns land at message boundaries: while it works, what it is saying now is still on its way.
    return status is AgentStatus.WORKING


def needs_you(status: AgentStatus) -> bool:
    """Whether the agent is stopped on something only a person can answer."""
    return status is AgentStatus.BLOCKED


def gone_quiet(quiet: float | None) -> bool:
    """Whether a working turn's log has been still long enough to say so instead of working."""
    return quiet is not None and quiet >= QUIET_AFTER


def cadence_for(agents: Iterable[Agent]) -> int:
    """Seconds between feed polls: faster while anything is working."""
    working = any(agent.status is AgentStatus.WORKING for agent in agents)
    return WORKING_CADENCE if working else IDLE_CADENCE


def _condition(age: float | None, cadence: int, *, failing: bool) -> Condition:
    if age is not None and age > DISCONNECTED_AFTER:
        return Condition.OFFLINE
    if failing:
        return Condition.DEGRADED
    if age is not None and age > 3 * cadence:
        return Condition.STALE
    return Condition.LIVE


def freshness(age: float | None, cadence: int, error: str | None) -> Freshness:
    """The feed's freshness, given the snapshot's age and the last refresh's failure class."""
    return Freshness(
        age=age,
        age_label="" if age is None else ago(age),
        condition=_condition(age, cadence, failing=error is not None),
    )


def agent_row(agent: Agent, overlay: Overlay, prompt: BlockedPrompt | None) -> AgentRow:
    """One agent's feed row, as its card reads it."""
    status = effective_status(agent, overlay)
    return AgentRow(
        session=agent.session,
        target=agent.target,
        title=agent.title,
        harness=agent.agent,
        workspace=agent.workspace,
        status=agent.status,
        effective_status=status,
        feed_key=agent.feed_key(rank(status)),
        answer=None if prompt is None else decide_screen(prompt),
        workspace_id=agent.workspace_id,
        stalled=agent.session in overlay.stalled and agent.status in _STALLABLE,
        pending=_pending(agent, overlay, status),
    )


def _pending(agent: Agent, overlay: Overlay, status: AgentStatus) -> Pending:
    # Only an idle card is marked: herdr's other states already say the agent is not resting.
    if status is not AgentStatus.IDLE:
        return NOTHING_PENDING
    return overlay.pending.get(str(agent.session), NOTHING_PENDING)


def _order(row: AgentRow) -> tuple[int, bool, tuple[int, int, int, str]]:
    # Within its status, a card with work still to come back leads; otherwise the feed's own order.
    return (row.feed_key[0], not row.pending, row.feed_key)


def _sections(rows: tuple[AgentRow, ...]) -> tuple[Section, ...]:
    # The blocked section shows even when empty: "nothing needs you" is itself news.
    cut = (
        Section(key, label, tuple(r for r in rows if r.effective_status is key))
        for key, label in SECTIONS
    )
    return tuple(s for s in cut if s.rows or s.key is AgentStatus.BLOCKED)


@dataclass(frozen=True, slots=True)
class Fetched:
    """When the herd was read: the injected ``now``, when the snapshot was taken, what failed.

    ``taken_at`` is ``None`` when no snapshot was ever read; ``error`` names the last refresh's
    failure class while the feed serves an older one.
    """

    now: float
    taken_at: float | None = None
    error: str | None = None

    @property
    def age(self) -> float | None:
        """How old the snapshot is, or ``None`` when there is none."""
        return None if self.taken_at is None else self.now - self.taken_at


def decide_herd(
    agents: Iterable[Agent],
    *,
    prompts: Mapping[str, BlockedPrompt],
    overlay: Overlay,
    fetched: Fetched,
) -> HerdDecision:
    """Order, section and tally the herd, with its freshness at the injected ``fetched.now``."""
    herd = tuple(agents)
    rows = tuple(
        sorted(
            (agent_row(agent, overlay, prompts.get(agent.session)) for agent in herd),
            key=_order,
        )
    )
    counts = dict.fromkeys(AgentStatus, 0)
    for row in rows:
        counts[row.effective_status] += 1
    cadence = cadence_for(herd)
    return HerdDecision(
        rows=rows,
        sections=_sections(rows),
        counts=counts,
        cadence=cadence,
        freshness=freshness(fetched.age, cadence, fetched.error),
    )


@dataclass(frozen=True, slots=True)
class Chip:
    """One workspace the feed can be narrowed to, with how many agents it holds."""

    workspace_id: WorkspaceId
    label: str
    count: int
    on: bool


@dataclass(frozen=True, slots=True)
class FeedView:
    """The herd as the feed draws it under a workspace filter."""

    sections: tuple[Section, ...]
    chips: tuple[Chip, ...]
    selected: str
    label: str
    counts: Mapping[AgentStatus, int]


def _chips(rows: tuple[AgentRow, ...], selected: str) -> tuple[Chip, ...]:
    counts: dict[WorkspaceId, int] = {}
    labels: dict[WorkspaceId, str] = {}
    for row in rows:
        if row.workspace_id:
            counts[row.workspace_id] = counts.get(row.workspace_id, 0) + 1
            labels.setdefault(row.workspace_id, row.workspace or row.workspace_id)
    chips = (Chip(ws, labels[ws], n, ws == selected) for ws, n in counts.items())
    # The chip that is on leads the strip, so the filter in force is never scrolled out of sight.
    return tuple(sorted(chips, key=lambda chip: not chip.on))


def _narrow(section: Section, selected: str) -> Section:
    # Needs-you is never filtered: a filter must not hide the agent waiting on you.
    if not selected or section.key is AgentStatus.BLOCKED:
        return section
    return Section(
        section.key, section.label, tuple(r for r in section.rows if r.workspace_id == selected)
    )


def _rest_counts(sections: tuple[Section, ...]) -> dict[AgentStatus, int]:
    # Needs-you is counted whole-herd on its own; these are the rest, in this workspace.
    rest = {s.key: s.count for s in sections if s.key is not AgentStatus.BLOCKED}
    return {**dict.fromkeys(AgentStatus, 0), **rest}


def feed_view(herd: HerdDecision, selected: str) -> FeedView:
    """The feed narrowed to one workspace; an unknown or empty ``selected`` shows everyone."""
    chips = _chips(herd.rows, selected)
    on = next((chip for chip in chips if chip.on), None)
    selected = on.workspace_id if on else ""
    narrowed = (_narrow(s, selected) for s in herd.sections)
    sections = tuple(s for s in narrowed if s.rows or s.key is AgentStatus.BLOCKED)
    counts = _rest_counts(sections) if on else herd.counts
    # One workspace leaves nothing to choose between.
    shown = chips if len(chips) > 1 else ()
    return FeedView(sections, shown, selected, on.label if on else "", counts)
