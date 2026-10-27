"""omp's Ask question as its session record wrote it, matched against the screen showing it."""

from __future__ import annotations

import re
from typing import Final

from pydantic import BaseModel, ConfigDict

from herdr_pwa import glyphs
from herdr_pwa.engine.question import QuestionMatch, QuestionOption, RecordedQuestion
from herdr_pwa.patterns import Rx
from herdr_pwa.validation import Flag

_OTHER_ROW: Final = "Other (type your own)"

# The keys omp's Ask footer may offer the phone; anything else there is not a control.
_KEYS: Final = frozenset({"enter", "esc", "up", "down"})

_TOP: Final = re.compile(r"^\s*╭─\s*Ask\b")
_BOTTOM: Final = re.compile(r"^\s*╰─+╯\s*$")
_BAR: Final = re.compile(r"^(?P<indent>\s*)[│┃║]\s?")
_TAIL: Final = re.compile(r"[│┃║]\s*$")
# A rule across the frame, which omp draws unbarred between the question, rows and footer.
_TEE: Final = re.compile(r"^\s*├.*┤\s*$")


class AskQuestion(BaseModel):
    """One question, exactly as the ask tool call's arguments spell it."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    question: str
    multi: Flag = None
    options: tuple[QuestionOption, ...] = ()


class OmpAsk(BaseModel):
    """The ask call still waiting on an answer, and the questions it holds."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    tool_call_id: str
    questions: tuple[AskQuestion, ...] = ()

    @property
    def menu(self) -> AskQuestion | None:
        """The one single-choice question this app draws, or ``None`` for any other shape."""
        if len(self.questions) != 1:
            return None
        question = self.questions[0]
        if question.multi is True or not question.options:
            return None
        if not all(option.label.strip() for option in question.options):
            return None
        return question

    @property
    def recorded(self) -> RecordedQuestion | None:
        """The menu as the engine's recorded question, under this call's id; else ``None``."""
        question = self.menu
        if question is None:
            return None
        return RecordedQuestion(
            id=self.tool_call_id, question=question.question, options=question.options
        )


def match(screen: str, question: RecordedQuestion) -> QuestionMatch | None:
    """The menu the record and the screen agree on, or ``None`` when any line differs."""
    texts = _frame(screen)
    if texts is None or not question.options:
        return None
    first = _first_row(texts, question.options[0].label)
    if first is None or _norm(" ".join(texts[: first[0]])) != _norm(question.question):
        return None
    walked = _walk(texts, question.options, *first)
    if walked is None:
        return None
    cursor, other = walked
    footer = tuple(texts[other + 1 :])
    other_row = _row(texts[other], _OTHER_ROW, first[1])
    if other_row is None or other_row[0] or not any(line.strip() for line in footer):
        return None
    # Its rows carry no numbers, so a digit typed there answers nothing.
    return QuestionMatch(question, cursor, footer, _KEYS, digits_move=True)


def _norm(text: str) -> str:
    """One line or paragraph with every run of whitespace folded to a space."""
    return " ".join(text.split())


def _expected(option: QuestionOption) -> str:
    """The text omp draws under one option: its description, then its preview."""
    return " ".join(
        part for part in (_norm(option.description or ""), _norm(option.preview or "")) if part
    )


def _drawn(lines: list[str]) -> str:
    """The text those lines draw, without the preview's own bar and with wrapping folded."""
    return _norm(" ".join(_BAR.sub("", line) for line in lines))


def _last_drawn(lines: list[str]) -> int | None:
    """The last line with anything on it, or ``None`` for an empty screen."""
    end = len(lines) - 1
    while end >= 0 and not lines[end].strip():
        end -= 1
    return end if end >= 0 else None


def _top(lines: list[str], end: int) -> int | None:
    """The frame's own top above *end*, skipping any box that closes before the bottom."""
    # Only an unbarred top is one omp drew; a description cannot draw the box's own side.
    tops = [i for i in range(end - 1, -1, -1) if _TOP.match(lines[i])]
    return next(
        (i for i in tops if not any(_BOTTOM.fullmatch(lines[j]) for j in range(i + 1, end))),
        None,
    )


def _inside(lines: list[str], top: int, end: int) -> list[str] | None:
    """The boxed text between *top* and *end*, or ``None`` when a line is not inside the box."""
    inside: list[str] = []
    for line in lines[top + 1 : end]:
        if not line.strip():
            inside.append("")
            continue
        # The frame's own rules carry no model text, so they are structure and not lines to walk.
        if _TEE.fullmatch(line):
            continue
        side = _BAR.match(line)
        if side is None:
            return None
        inside.append(_TAIL.sub("", line[side.end() :]).rstrip())
    return inside


def _frame(screen: str) -> list[str] | None:
    """The boxed lines inside the Ask frame that closes the screen, or ``None`` if it is not whole."""
    lines = screen.splitlines()
    end = _last_drawn(lines)
    if end is None or not _BOTTOM.fullmatch(lines[end]):
        return None
    top = _top(lines, end)
    return None if top is None else _inside(lines, top, end)


def _row(line: str, label: str, column: int | None = None) -> tuple[bool, int] | None:
    """The mark column and whether the cursor is on this row, or ``None`` for any other line."""
    stripped = line.rstrip()
    at = stripped.find("○")
    if at < 0 or (column is not None and at != column):
        return None
    prefix = stripped[:at]
    if stripped[at + 1 :].lstrip() != label.strip():
        return None
    if not prefix.strip():
        return False, at
    return (True, at) if prefix.strip() in glyphs.CURSOR else None


def _first_row(texts: list[str], label: str, column: int | None = None) -> tuple[int, int] | None:
    """The first line that draws *label*'s row, with its line and mark column."""
    for index, line in enumerate(texts):
        hit = _row(line, label, column)
        if hit is not None:
            return index, hit[1]
    return None


def _walk(
    texts: list[str], options: tuple[QuestionOption, ...], index: int, column: int
) -> tuple[int, int] | None:
    """Check each option's row and the text under it; return the cursor's row and Other's line."""
    cursors: list[int] = []
    following = [*(option.label for option in options[1:]), _OTHER_ROW]
    for number, (option, after) in enumerate(zip(options, following, strict=True)):
        row = _row(texts[index], option.label, column)
        found = _first_row(texts[index + 1 :], after, column)
        if row is None or found is None:
            return None
        end = index + 1 + found[0]
        if _drawn(texts[index + 1 : end]) != _expected(option):
            return None
        if row[0]:
            cursors.append(number)
        index = end
    # Exactly one cursor: none, or two, is a screen the record does not explain.
    return (cursors[0], index) if len(cursors) == 1 else None


PATTERNS: Final = (
    Rx(
        _TOP,
        ("╭─ Ask ─────╮", "  ╭─ Ask 1 questions", "╭─ Ask"),
        ("╭─── Ask 1 questions ─╮", "│ ╭─ Ask ─╮", "╭─ Asks"),
        "match",
    ),
    Rx(
        _BOTTOM,
        ("╰────╯", "  ╰─╯  ", "╰─╯"),
        ("├────┤", "╰────", "╰╯", "╰────╯ extra"),
        "fullmatch",
    ),
    Rx(
        _BAR,
        ("│ text", "  ┃ x", "   ║", "│x"),
        ("text │", "> x", "x"),
        "match",
    ),
    Rx(
        _TAIL,
        ("text │", "x ┃  ", "│"),
        ("text |", "│ x", "x"),
    ),
    Rx(
        _TEE,
        ("├────┤", "  ├─┤  ", "├── title ──┤"),
        ("│ ├──┤ │", "├──", "──┤"),
        "fullmatch",
    ),
)
