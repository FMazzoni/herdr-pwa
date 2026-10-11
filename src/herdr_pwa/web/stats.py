"""The stats strip: what an agent's own harness records about its model, context, turn and spend.

Nothing here is estimated. A field whose harness records no source for it is left out, never shown
as zero: money only where the harness wrote it down, a context percentage only for a model whose
window is in ``WINDOWS``.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from herdr_pwa.engine.transcript import HistoryState, ModelRef, SessionUsage

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
_MINUTE: Final = 60
_HOUR: Final = 3600
_DAY: Final = 86400


@dataclass(frozen=True, slots=True)
class Row:
    """One line of the details sheet."""

    label: str
    value: str


@dataclass(frozen=True, slots=True)
class Stats:
    """The strip and its sheet, every field already worded; an empty field is not shown."""

    model: str = ""
    percent: int | None = None
    meter: int = 0
    context: str = ""
    turn: str = ""
    spend: str = ""
    rows: tuple[Row, ...] = ()
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


def tokens_said(count: int) -> str:
    """A token count as VoiceOver should say it."""
    if count >= _MILLION:
        return f"{count / _MILLION:.1f} million"
    if count >= _THOUSAND:
        return f"{round(count / _THOUSAND)} thousand"
    return str(count)


def duration(seconds: float) -> str:
    """A span at a glance: 40s, 4m, 1h 12m."""
    whole = int(seconds)
    if whole < _MINUTE:
        return f"{whole}s"
    if whole < _HOUR:
        return f"{whole // _MINUTE}m"
    if whole < _DAY:
        return f"{whole // _HOUR}h {whole % _HOUR // _MINUTE}m"
    return f"{whole // _DAY}d {whole % _DAY // _HOUR}h"


def _counted(count: int, unit: str) -> str:
    return f"{count} {unit}{'' if count == 1 else 's'}"


def duration_said(seconds: float) -> str:
    """A span as VoiceOver should say it."""
    whole = int(seconds)
    if whole < _MINUTE:
        return "under a minute"
    if whole < _HOUR:
        return _counted(whole // _MINUTE, "minute")
    if whole < _DAY:
        return f"{_counted(whole // _HOUR, 'hour')} {_counted(whole % _HOUR // _MINUTE, 'minute')}"
    return f"{_counted(whole // _DAY, 'day')} {_counted(whole % _DAY // _HOUR, 'hour')}"


def _seconds_since(stamp: str, now: float) -> float | None:
    """How long ago an ISO 8601 stamp was, or None when it is unreadable or in the future."""
    try:
        moment = datetime.fromisoformat(stamp).timestamp()
    except ValueError:
        return None
    return now - moment if now >= moment else None


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


def _newest(usage: SessionUsage) -> ModelRef:
    return usage.models[-1] if usage.models else ModelRef()


def _context(kind: str, usage: SessionUsage) -> _Context | None:
    # hermes draws a context figure on screen and stores nothing behind it.
    if kind == "hermes" or not usage.context_tokens:
        return None
    return _Context(usage.context_tokens, window_of(_newest(usage).model))


def _turn(usage: SessionUsage, status: str, now: float) -> float | None:
    if status != "working" or not usage.turn_started:
        return None
    return _seconds_since(usage.turn_started, now)


@dataclass(frozen=True, slots=True)
class _Found:
    """What the tally answers, before it is worded."""

    model: ModelRef
    context: _Context | None
    turn: float | None
    cost: float
    usage: SessionUsage
    idle: float | None


def _found(kind: str, status: str, usage: SessionUsage, now: float) -> _Found:
    return _Found(
        model=_newest(usage),
        context=_context(kind, usage),
        turn=_turn(usage, status, now),
        # Zero is the tally's word for "the harness recorded no money"; claude records none.
        cost=usage.cost if kind != "claude" else 0.0,
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
        return f"${found.cost:.2f}"
    if found.usage.output_tokens:
        return f"{tokens(found.usage.output_tokens)} out"
    return ""


def _token_rows(usage: SessionUsage) -> list[Row]:
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
        rows.append(Row("Cost", f"${found.cost:.2f}"))
    if found.idle is not None:
        rows.append(Row("Last activity", f"{duration(found.idle)} ago"))
    if found.usage.prompts:
        rows.append(Row("Turns", str(found.usage.prompts)))
    if found.usage.partial:
        rows.append(Row("Counted", "the newest 64 MB only, so these are at least the totals"))
    return tuple(rows)


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
        said.append(f"spent ${found.cost:.2f}")
    elif found.usage.output_tokens:
        said.append(f"{tokens_said(found.usage.output_tokens)} output tokens")
    return ", ".join(said)


_QUIET: Final = {
    HistoryState.unsupported: "No usage from this harness",
    HistoryState.unreadable: "Usage could not be read",
}


def stats_for(agent_kind: str, status: str, usage: SessionUsage, *, now: float) -> Stats:
    """The strip for one agent, from its harness's tally, at wall-clock *now*."""
    kind = agent_kind.strip().lower()
    found = _found(kind, status, usage, now)
    sentence = _sentence(found)
    if not sentence:
        note = _QUIET.get(usage.state, "No usage recorded yet")
        return Stats(rows=_rows(found), sentence=f"Stats: {note.lower()}", note=note)
    context = found.context
    percent = context.percent if context is not None else None
    return Stats(
        model=model_label(found.model.model) if found.model.model else "",
        percent=percent,
        # Half up, so a quarter full draws three tenths, never banker's two.
        meter=(percent * METER_STEPS + 50) // 100 if percent is not None else 0,
        context=_context_text(context) if context is not None else "",
        turn=duration(found.turn) if found.turn is not None else "",
        spend=_spend(found),
        rows=_rows(found),
        sentence=sentence,
    )
