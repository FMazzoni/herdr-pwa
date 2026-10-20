"""The stats strip: what an agent's own harness records about its model, context, turn and spend.

Nothing here is estimated. A field whose harness records no source for it is left out, never shown
as zero: money only where the harness wrote it down, a context percentage only for a model whose
window is in ``WINDOWS``.
"""

import re
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Final

from herdr_pwa.clock import ago, brief, duration, duration_said
from herdr_pwa.engine import (
    SPAWNED_AGENT,
    Harness,
    HistoryState,
    ModelRef,
    ModelSpend,
    SessionUsage,
    SpawnedAgent,
    harness_of,
)
from herdr_pwa.panetext import Run, plain_text, segments

# Context windows published for models that have no larger variant, so a percentage of them is
# true. A model not listed here shows its context in tokens, with no percentage and no bar.
# Source: Anthropic's models overview. Sonnet 4 and later Opus models are left out on purpose:
# they also run with a 1M window, and the transcript does not say which one a session has.
WINDOWS: Final[tuple[tuple[re.Pattern[str], int], ...]] = (
    (re.compile(r"claude-haiku-4-5(-\d{8})?"), 200_000),
    (re.compile(r"claude-opus-4-5(-\d{8})?"), 200_000),
    (re.compile(r"claude-opus-4-1(-\d{8})?"), 200_000),
    (re.compile(r"claude-opus-4-\d{8}"), 200_000),
    (re.compile(r"claude-3-7-sonnet(-\d{8})?"), 200_000),
    (re.compile(r"claude-3-5-(sonnet|haiku)(-\d{8})?"), 200_000),
)
# The bar is drawn in tenths: a class per step, since no inline style may set a width.
METER_STEPS: Final = 10
MAX_MODEL_CHARS: Final = 40
_CLAUDE: Final = re.compile(r"claude-(opus|sonnet|haiku)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?")
_DATED: Final = re.compile(r"-\d{8}$")
_THOUSAND: Final = 1_000
_MILLION: Final = 1_000_000
# Past this a scaled count drops its decimal: 9.9k, then 10k.
_ONE_PLACE: Final = 9.95
# One model is the session itself; the split is drawn from two.
_SPLIT: Final = 2
# Below this, two places would round a real charge down to $0.00.
_HALF_CENT: Final = 0.005


@dataclass(frozen=True, slots=True)
class Row:
    """One line of the details sheet."""

    label: str
    value: str


@dataclass(frozen=True, slots=True)
class StatusRow:
    """One row of the statusline the terminal shows: its wrap pieces, and its text for VoiceOver."""

    segments: tuple[tuple[Run, ...], ...]
    text: str


@dataclass(frozen=True, slots=True)
class Stats:
    """The strip and its sheet, every field already worded; an empty field is not shown."""

    # The harness's own statusline when the pane shows one: its first row is the line, cut at
    # the phone's width; every row is in the sheet whole, in place of the facts below.
    status: tuple[StatusRow, ...] = ()
    model: str = ""
    percent: int | None = None
    meter: int = 0
    context: str = ""
    turn: str = ""
    spend: str = ""
    rows: tuple[Row, ...] = ()
    # One line per model, only when more than one answered; drawn across the sheet's width.
    models: tuple[Row, ...] = ()
    sentence: str = ""
    note: str = ""


def window_of(model: str) -> int:
    """The model's context window in tokens, or 0 when it is not known."""
    for pattern, size in WINDOWS:
        if pattern.fullmatch(model):
            return size
    return 0


def model_label(model: str) -> str:
    """A model id as a person says it: ``claude-opus-4-5-20251101`` is "Opus 4.5"."""
    name = model.strip().rsplit("/", 1)[-1]
    found = _CLAUDE.fullmatch(name)
    if found:
        family, major, minor = found.groups()
        version = f"{major}.{minor}" if minor else major
        return f"{family.capitalize()} {version}"
    return _DATED.sub("", name)[:MAX_MODEL_CHARS]


def tokens(count: int) -> str:
    """A token count at a glance: 812, 1.4k, 597k, 2.3M."""
    for step, suffix in ((_MILLION, "M"), (_THOUSAND, "k")):
        # 0.9995 rather than 1, so 999,999 reads 1.0M and not 1000k.
        if count >= step * 0.9995:
            scaled = count / step
            return f"{scaled:.1f}{suffix}" if scaled < _ONE_PLACE else f"{scaled:.0f}{suffix}"
    return str(count)


def money(cost: float) -> str:
    """Dollars at a glance, where a charge too small for cents is still not $0.00."""
    return "under $0.01" if cost < _HALF_CENT else f"${cost:.2f}"


def tokens_said(count: int) -> str:
    """A token count as VoiceOver should say it."""
    if count >= _MILLION:
        return f"{count / _MILLION:.1f} million"
    if count >= _THOUSAND:
        return f"{round(count / _THOUSAND)} thousand"
    return str(count)


def _seconds_since(stamp: str, now: float) -> float | None:
    """How long ago an ISO 8601 stamp was, or None when it is unreadable or in the future."""
    try:
        moment = datetime.fromisoformat(stamp).timestamp()
    except ValueError:
        return None
    return now - moment if now >= moment else None


@dataclass(frozen=True, slots=True)
class TurnTimes:
    """The newest turn's clock, in seconds: how long it has run, how long its log has been still,
    and how long it took from the prompt to the last record; ``None`` where the tally cannot say."""

    ran: float | None = None
    quiet: float | None = None
    took: float | None = None


# A turn the tally can say nothing about.
NO_TIMES: Final = TurnTimes()


def turn_times(usage: SessionUsage, now: float) -> TurnTimes:
    """The newest turn's clock from the tally the strip reads, at wall-clock *now*."""
    ran = _seconds_since(usage.turn_started, now) if usage.turn_started else None
    quiet = _seconds_since(usage.last_at, now) if usage.last_at else None
    took = ran - quiet if ran is not None and quiet is not None and ran >= quiet else None
    return TurnTimes(ran=ran, quiet=quiet, took=took)


@dataclass(frozen=True, slots=True)
class SpawnedTime:
    """A background agent the turn has on its hands, and how long it ran or has been running."""

    description: str
    running: bool
    failed: bool = False
    seconds: float | None = None
    kind: str = SPAWNED_AGENT
    # Started since the newest prompt: work the last turn set going.
    fresh: bool = False


def _fresh(agent: SpawnedAgent, turn_started: str) -> bool:
    return bool(turn_started) and (agent.resumed or agent.started) >= turn_started


def _spawned_time(agent: SpawnedAgent, now: float, turn_started: str) -> SpawnedTime:
    # A resumed agent's run is the one since it was woken.
    since = _seconds_since(agent.resumed or agent.started, now)
    if agent.done:
        until = _seconds_since(agent.ended, now)
        since = since - until if since is not None and until is not None else None
    fresh = _fresh(agent, turn_started)
    return SpawnedTime(agent.description, not agent.done, agent.failed, since, agent.kind, fresh)


def _before(stamp: str, since: float | None) -> bool:
    if since is None:
        return False
    try:
        return datetime.fromisoformat(stamp).timestamp() < since
    except ValueError:
        return False


def _this_turn(agent: SpawnedAgent, turn_started: str, since: float | None) -> bool:
    """Still running, or ended since the newest prompt; a launch never seen is not listed, nor
    one still running that the agent's process was started after, which died with the last."""
    if not agent.started:
        return False
    if not agent.done:
        return not _before(agent.resumed or agent.started, since)
    return bool(turn_started) and agent.ended >= turn_started


def spawned_times(
    usage: SessionUsage, now: float, *, since: float | None = None
) -> tuple[SpawnedTime, ...]:
    """The tally's background agents and shells still running, the last turn's first, then those
    that ended this turn, each by when it started; *since* is when the agent's process started,
    when known."""
    turn = usage.turn_started
    shown = [agent for agent in usage.agents if _this_turn(agent, turn, since)]
    # Running first; of those, what the last turn started before what an older one left.
    shown.sort(key=lambda agent: (agent.done, not _fresh(agent, turn), agent.started))
    return tuple(_spawned_time(agent, now, turn) for agent in shown)


@dataclass(frozen=True, slots=True)
class _Context:
    used: int
    window: int

    @property
    def percent(self) -> int | None:
        # A prompt larger than the listed window means this session runs a larger one.
        if not self.window or self.used > self.window:
            return None
        return round(100 * self.used / self.window)


def _context(harness: Harness | None, usage: SessionUsage) -> _Context | None:
    if (harness is not None and not harness.stores_context) or not usage.context_tokens:
        return None
    return _Context(usage.context_tokens, window_of(usage.latest.model))


@dataclass(frozen=True, slots=True)
class Reading:
    """One agent's tally as the strip reads it: its harness, its status word, and when it is."""

    agent_kind: str
    status: str
    usage: SessionUsage
    # Wall-clock seconds, injected, so the strip says how long ago against a held clock.
    now: float

    @property
    def kind(self) -> str:
        """The harness name as the registries spell it."""
        return self.agent_kind.strip().lower()


def _turn(reading: Reading) -> float | None:
    usage = reading.usage
    if reading.status != "working" or not usage.turn_started:
        return None
    return _seconds_since(usage.turn_started, reading.now)


@dataclass(frozen=True, slots=True)
class _Found:
    """What the tally answers, before it is worded."""

    model: ModelRef
    context: _Context | None
    turn: float | None
    cost: float
    usage: SessionUsage
    idle: float | None


def _priced(harness: Harness | None) -> bool:
    return harness is None or harness.prices_itself


def _found(reading: Reading) -> _Found:
    harness, usage, now = harness_of(reading.agent_kind), reading.usage, reading.now
    return _Found(
        model=usage.latest,
        context=_context(harness, usage),
        turn=_turn(reading),
        # Zero is the tally's word for "the harness recorded no money".
        cost=usage.cost if _priced(harness) else 0.0,
        usage=usage,
        idle=_seconds_since(usage.last_at, now) if usage.last_at else None,
    )


def _context_text(context: _Context) -> str:
    if context.percent is None:
        return f"{tokens(context.used)} ctx"
    return f"{context.percent}%"


def _context_row(context: _Context) -> Row:
    if context.percent is None:
        return Row("Context", f"{tokens(context.used)} used; this model's window is not known")
    return Row(
        "Context", f"{tokens(context.used)} of {tokens(context.window)} ({context.percent}%)"
    )


def _spend(found: _Found) -> str:
    if found.cost > 0:
        return money(found.cost)
    if found.usage.output_tokens:
        return f"{tokens(found.usage.output_tokens)} out"
    return ""


def _providers(usage: SessionUsage) -> str:
    named = dict.fromkeys(row.ref.provider for row in usage.by_model if row.ref.provider)
    return " and ".join(named) or "the provider"


def _token_rows(usage: SessionUsage) -> list[Row]:
    if not usage.recorded:
        return [Row("Tokens", f"not recorded by {_providers(usage)}")]
    named = (
        ("Input", usage.input_tokens),
        ("Output", usage.output_tokens),
        ("Thinking", usage.thinking_tokens),
        ("Cache read", usage.cache_read_tokens),
        ("Cache written", usage.cache_creation_tokens),
    )
    # Input and output are always recorded with a message; the rest only when used.
    return [
        Row(label, tokens(count)) for label, count in named if count or label in ("Input", "Output")
    ]


def _rows(found: _Found) -> tuple[Row, ...]:
    rows: list[Row] = []
    if found.model.model:
        provider = f" via {found.model.provider}" if found.model.provider else ""
        rows.append(Row("Model", f"{model_label(found.model.model)}{provider}"))
    if found.context is not None:
        rows.append(_context_row(found.context))
    if found.usage.messages:
        rows.extend(_token_rows(found.usage))
    if found.cost > 0:
        rows.append(Row("Cost", money(found.cost)))
    if found.turn is not None:
        rows.append(Row("Turn", duration(found.turn)))
    if found.idle is not None:
        rows.append(Row("Last activity", ago(found.idle)))
    if found.usage.prompts:
        rows.append(Row("Turns", str(found.usage.prompts)))
    if found.usage.partial:
        rows.append(Row("Counted", "the newest 64 MB only, so these are at least the totals"))
    rows.extend(_left_out(found.usage))
    return tuple(rows)


def _named(ref: ModelRef) -> str:
    return (
        f"{model_label(ref.model)} via {ref.provider}" if ref.provider else model_label(ref.model)
    )


def _left_out(usage: SessionUsage) -> list[Row]:
    # Only beside a model that did record: alone, the token row already says it.
    silent = [_named(row.ref) for row in usage.by_model if not row.recorded]
    if not silent or not usage.recorded:
        return []
    them = "them" if len(silent) > 1 else "it"
    return [Row("Not recorded", f"{', '.join(silent)}, so the totals leave {them} out")]


def _figures(spend: ModelSpend) -> str:
    named = (
        ("in", spend.input_tokens),
        ("out", spend.output_tokens),
        ("thinking", spend.thinking_tokens),
        ("cache read", spend.cache_read_tokens),
        ("cache written", spend.cache_creation_tokens),
    )
    return ", ".join(
        f"{tokens(count)} {label}" for label, count in named if count or label in ("in", "out")
    )


def _counted(count: int, unit: str) -> str:
    return f"{count} {unit}{'' if count == 1 else 's'}"


def _model_line(spend: ModelSpend, *, priced: bool) -> Row:
    answers = _counted(spend.messages, "answer")
    if not spend.recorded:
        return Row(_named(spend.ref), f"{answers}; no tokens or cost recorded")
    cost = money(spend.cost) if priced and spend.cost > 0 else "no cost recorded"
    return Row(_named(spend.ref), f"{answers}: {_figures(spend)}; {cost}")


def _models(reading: Reading) -> tuple[Row, ...]:
    split = reading.usage.by_model
    if len(split) < _SPLIT:
        return ()
    priced = _priced(harness_of(reading.agent_kind))
    return tuple(_model_line(spend, priced=priced) for spend in split)


def _sentence(found: _Found) -> str:
    said: list[str] = []
    if found.model.model:
        said.append(model_label(found.model.model))
    context = found.context
    if context is not None:
        percent = context.percent
        said.append(
            f"context {percent} percent used"
            if percent is not None
            else f"context {tokens_said(context.used)} tokens"
        )
    if found.turn is not None:
        said.append(f"turn running {duration_said(found.turn)}")
    if found.cost > 0:
        said.append(f"spent {money(found.cost)}")
    elif found.usage.output_tokens:
        said.append(f"{tokens_said(found.usage.output_tokens)} output tokens")
    return ", ".join(said)


_QUIET: Final = {
    HistoryState.unsupported: "No usage from this harness",
    HistoryState.unreadable: "Usage could not be read",
}


def stats_for(reading: Reading) -> Stats:
    """The strip for one agent, from its harness's tally, at the reading's wall-clock time."""
    found = _found(reading)
    sentence = _sentence(found)
    if not sentence:
        note = _QUIET.get(reading.usage.state, "No usage recorded yet")
        return Stats(
            rows=_rows(found),
            models=_models(reading),
            sentence=f"Stats: {note.lower()}",
            note=note,
        )
    context = found.context
    percent = context.percent if context is not None else None
    return Stats(
        model=model_label(found.model.model) if found.model.model else "",
        percent=percent,
        # Half up, so a quarter full draws three tenths, never banker's two.
        meter=(percent * METER_STEPS + 50) // 100 if percent is not None else 0,
        context=_context_text(context) if context is not None else "",
        turn=brief(found.turn) if found.turn is not None else "",
        spend=_spend(found),
        rows=_rows(found),
        models=_models(reading),
        sentence=sentence,
    )


def _status_row(row: list[Run]) -> StatusRow:
    pieces = tuple(tuple(piece) for piece in segments(row))
    # Said from the pieces, so a glyph the phone does not draw is not read out either.
    return StatusRow(
        pieces, " ".join(" ".join(plain_text(list(piece)).split()) for piece in pieces)
    )


def with_statusline(stats: Stats, rows: list[list[Run]]) -> Stats:
    """*stats* with the statusline the pane shows drawn in place of its facts; unchanged if none."""
    if not rows:
        return stats
    status = tuple(_status_row(row) for row in rows)
    # The strip says what the terminal says, so VoiceOver hears those words and not a tally's.
    return replace(
        stats, status=status, sentence="Statusline: " + "; ".join(r.text for r in status)
    )
