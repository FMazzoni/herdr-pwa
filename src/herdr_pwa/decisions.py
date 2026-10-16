"""What the dashboard decides about a screen and a herd, as typed records the views render."""

import posixpath
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from herdr_pwa.engine import (
    Agent,
    AgentStatus,
    BlockedPrompt,
    KeyHint,
    MenuShape,
    PaneId,
    SessionId,
    rank,
)
from herdr_pwa.panetext import printed_whole

IDLE_CADENCE: Final = 12
WORKING_CADENCE: Final = 5
# Past this many seconds the herd is offline rather than merely late.
DISCONNECTED_AFTER: Final = 60
# A two-digit option needs two keystrokes, and the first one already answers row 1.
MAX_TAPPABLE_OPTION: Final = 9
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
    digits: tuple[str, ...]
    rows_answer: bool
    rows_toggle: bool
    hints: tuple[KeyHint, ...]
    answer_hints: tuple[KeyHint, ...]
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
    disconnected: bool
    degraded: bool
    failure: str

    @property
    def stale(self) -> bool:
        """Every condition but live dims the feed: what is shown may not be now."""
        return self.condition is not Condition.LIVE


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
    workspace_id: str = ""


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
    if _options_refused(prompt):
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
        digits=digits,
        rows_answer=rows_answer(prompt),
        rows_toggle=rows_toggle(prompt),
        hints=prompt.hints,
        answer_hints=answer_hints(prompt, digits=digits),
        steps=None if stepper is None else StepCount(stepper.answered, stepper.total),
        corroborated=prompt.corroborated,
        drew_rows=prompt.drew_rows,
        asked=prompt.asked,
        is_choice=prompt.is_choice,
        focus=prompt.focus,
    )


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


def effective_status(agent: Agent, unread: Mapping[str, bool] | None = None) -> AgentStatus:
    """The status the feed acts on: a finished agent already read is idle again."""
    if agent.status is not AgentStatus.DONE or unread is None:
        return agent.status
    # A session missing from the seen ledger has never been read.
    return agent.status if unread.get(agent.session, True) else AgentStatus.IDLE


def cadence_for(agents: Iterable[Agent]) -> int:
    """Seconds between feed polls: faster while anything is working."""
    working = any(agent.status is AgentStatus.WORKING for agent in agents)
    return WORKING_CADENCE if working else IDLE_CADENCE


def _age_label(age: float) -> str:
    if age < 60:  # noqa: PLR2004 -- seconds in a minute
        return f"{age:.0f}s ago"
    if age < 3600:  # noqa: PLR2004 -- seconds in an hour
        return f"{age / 60:.0f}m ago"
    return f"{age / 3600:.0f}h ago"


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
        age_label="" if age is None else _age_label(age),
        condition=_condition(age, cadence, failing=error is not None),
        disconnected=age is not None and age > DISCONNECTED_AFTER,
        degraded=error is not None,
        failure=error or "",
    )


def _row(agent: Agent, status: AgentStatus, prompt: BlockedPrompt | None) -> AgentRow:
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
    )


def _sections(rows: tuple[AgentRow, ...]) -> tuple[Section, ...]:
    # The blocked section shows even when empty: "nothing needs you" is itself news.
    cut = (
        Section(key, label, tuple(r for r in rows if r.effective_status is key))
        for key, label in SECTIONS
    )
    return tuple(s for s in cut if s.rows or s.key is AgentStatus.BLOCKED)


def decide_herd(  # noqa: PLR0913 -- every input is a separate fact the feed is decided from
    agents: Iterable[Agent],
    *,
    prompts: Mapping[str, BlockedPrompt],
    unread: Mapping[str, bool] | None,
    now: float,
    taken_at: float | None,
    error: str | None,
) -> HerdDecision:
    """Order, section and tally the herd, with its freshness at the injected ``now``."""
    herd = tuple(agents)
    rows = tuple(
        sorted(
            (
                _row(agent, effective_status(agent, unread), prompts.get(agent.session))
                for agent in herd
            ),
            key=lambda row: row.feed_key,
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
        freshness=freshness(None if taken_at is None else now - taken_at, cadence, error),
    )


@dataclass(frozen=True, slots=True)
class Chip:
    """One workspace the feed can be narrowed to, with how many agents it holds."""

    workspace_id: str
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
    counts: dict[str, int] = {}
    labels: dict[str, str] = {}
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
