"""Reading a blocked agent's screen: the question, its options, its key hints."""

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from string import ascii_lowercase
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, field_validator

from herdr_pwa import facts, glyphs
from herdr_pwa.engine.model import PaneId, SessionId, as_pane_id, as_session_id, screen_digest
from herdr_pwa.panetext import as_text_read, composer_top, escape_at, plain
from herdr_pwa.patterns import alternatives

_EDGE_RE: Final = re.compile(rf"^(?P<indent>\s*){glyphs.one_of(glyphs.SIDE, glyphs.LEFT_BLOCKS)}")
"""A box-edge bar at line start, with the indent that owns it.

opencode draws its dialogs with a left border only (SplitBorder in its TUI
source), so every row opens with the bar; the grammar anchors at line start and
must see past it. The spaces after the bar stay, so an option's indented
description keeps its depth.
"""

OPTION_RE: Final = re.compile(
    rf"""
    \s*+ {glyphs.one_of(glyphs.QUOTE_CURSOR, glyphs.CURSOR, "*-•")}?  # a cursor or a bullet
    \s*+ (?P<open>\[)? (?P<number>\d{{1,2}}) (?(open)\]|\s*[.):])  # 1. or 2) or [3], closed alike
    \s+ (?P<label>\S(?:.*\S)?) \s*  # the label, first to last letter, without the padding
    """,
    re.VERBOSE,
)
"""One numbered row: ``1. Yes``, ``2) No``, or hermes's bracketed ``[1] Approve Once``.

The bracket must close on the same number it opened on, so ``[1. x`` and ``1] x``
are not rows: a half-bracketed number is prose, not a menu.
"""

_DIVIDER_RE: Final = re.compile(
    rf"[\s{re.escape(glyphs.RULE + glyphs.DASHED_RULE + '—=_·•-')}]{{4,}}"
)

# A footer names a key and then, later on the same line, what it does; read as two scans, since
# one pattern bridging them with ``.*`` retried every key it passed (5.5 s on a 30k-character line).
_FOOTER_KEY: Final = re.compile(
    rf"\b(?:{alternatives(facts.FOOTER_KEY_WORDS)})\b|{alternatives(facts.FOOTER_KEY_MARKS)}",
    re.IGNORECASE,
)
_FOOTER_VERB: Final = re.compile(alternatives(facts.FOOTER_VERBS), re.IGNORECASE)


def _names_key_then_verb(text: str) -> bool:
    """Does a line of *text* name a key and then, after it, a verb?"""
    for line in text.split("\n"):
        # The first key ends first, since no key's name holds another, so it is the one to try.
        key = _FOOTER_KEY.search(line)
        if key is not None and _FOOTER_VERB.search(line, key.end()) is not None:
            return True
    return False


_MAX_CHROME_PARAGRAPHS: Final = 3

_MIN_MENU_OPTIONS: Final = 2

_MIN_CHECKBOX_ROWS: Final = 2


_TICKED_BOX: Final = "xX*" + glyphs.CHECK

_BLANK_BOX: Final = " ·"

_CHECKBOX_RE: Final = re.compile(
    rf"""
    \s*
    (?: \[ (?P<box>{glyphs.one_of(_TICKED_BOX, _BLANK_BOX)}?) \]  # a mark in brackets, [x] or [ ]
      | (?P<glyph>{glyphs.one_of(glyphs.TICKED_BOX, glyphs.BLANK_BOX)})  # or a checkbox glyph
    )
    \s* (?P<rest>\S(?:.*\S)?) \s*  # the label beside it
    """,
    re.VERBOSE,
)


def _ticked(label: str) -> tuple[bool | None, str]:
    """Split ``"[✔] autumn"`` into the tick state and the label the terminal drew."""
    match = _CHECKBOX_RE.fullmatch(label)
    if match is None:
        return None, label
    mark = match.group("box") or match.group("glyph") or ""
    return bool(mark) and mark in _TICKED_BOX + glyphs.TICKED_BOX, match.group("rest")


_MARKER_RE: Final = re.compile(rf"^\s*{glyphs.one_of(glyphs.QUOTE_CURSOR, glyphs.CURSOR)}\s")

_PARKED_RE: Final = re.compile(rf"^{glyphs.one_of(glyphs.CURSOR)}\s+\S")
"""The cursor parked on a menu's own unnumbered row, such as claude's checklist ``Next``.

Only a glyph in the first column counts: ``>`` there is a markdown quote in a
recap, and an indented glyph is body text under a row.
"""

_STEP_EDGES: Final = glyphs.ARROW_LEFT + glyphs.ARROW_RIGHT

_STEP_TOKEN_RE: Final = re.compile(
    rf"""
    (?P<box>{glyphs.one_of(glyphs.STEP_BOXES)})  # a step's box
    \s* (?P<label>[^{re.escape(glyphs.STEP_BOXES + _STEP_EDGES)}]*)  # its label, up to the next
    """,
    re.VERBOSE,
)

_STEPPER_RE: Final = re.compile(
    rf"""
    {glyphs.ARROW_LEFT} [^{_STEP_EDGES}]*  # the strip opens on an arrow
    {glyphs.one_of(glyphs.STEP_BOXES)}  # holds at least one step box
    [^{_STEP_EDGES}]* {glyphs.ARROW_RIGHT}  # and closes on the other arrow
    """,
    re.VERBOSE,
)


_HINT_RE: Final = re.compile(
    r"""
    \s* (?P<keys>[^\s].{0,29}?)  # the keys, a few words at most
    \s+ to \s+
    (?P<verb>[A-Za-z][A-Za-z0-9 /+-]{0,28}?) \s*  # what they do
    """,
    re.VERBOSE,
)
"""One ``<keys> to <verb>`` hint, the form most harnesses print."""


_VERB_HINT_RE: Final = re.compile(
    r"""
    \s* (?P<keys>[^\s].{0,29}?)  # the keys, a few words at most
    \s+ (?P<verb>[A-Za-z][A-Za-z0-9 /+-]{0,28}?) \s*  # then what they do, with no "to"
    """,
    re.VERBOSE,
)
"""One verb-first hint: ``<keys> <verb>`` with no "to" at all.

The verb-first form is trusted only for the verbs opencode is measured to
print (its TUI source and the live captures agree), because the same shape is
what status bars print -- claude's ``← for agents`` is not an offer.
"""


def _hint_match(text: str) -> "re.Match[str] | None":
    """One hint out of a footer piece, the "to" form taking precedence.

    Trying the plain-space join first mis-splits ``tab or esc to clear notes``
    into keys="tab" and a verb that swallows the rest; the "to" form is the
    more specific one and is tried first.
    """
    if (m := _HINT_RE.fullmatch(text)) is not None:
        return m
    if (m := _VERB_HINT_RE.fullmatch(text)) is not None and m.group(
        "verb"
    ).lower() in facts.VERB_FIRST_VERBS:
        return m
    return None


_HINT_SPLIT: Final = re.compile(r"\s*[·•|\uff5c]\s*|\s{2,}")
"""What a terminal separates footer hints with.

opencode puts two spaces between hint pairs on the same line (measured on its
permission dialog, where the buttons share that line).
"""


_TRAILING_PAREN_RE: Final = re.compile(r"(?<=\S)\s*\([^()]*\)\s*$")


def _hint_text(segment: str) -> str:
    """One footer piece with its wrapping and any trailing countdown removed."""
    return _TRAILING_PAREN_RE.sub("", segment.strip()).strip(" ()[]")


def _whole_hint(text: str) -> bool:
    """Does *text* parse as ONE hint whose keys are all really keys?"""
    match = _hint_match(text)
    if match is None:
        return False
    pieces = _key_pieces(match.group("keys"))
    return bool(pieces) and all(_KEY_TOKEN_RE.fullmatch(piece) for piece in pieces)


_KEY_PIECE: Final = re.compile(r"\s*/\s*|\s+or\s+|\s*,\s*")

_KEY_NAMES: Final = tuple(
    name for name in (*facts.KEY_ALIASES, *facts.UNSENT_KEYS) if " " not in name
)

_KEY_TOKEN_RE: Final = re.compile(
    rf"""
    (?: (?:{alternatives(facts.KEY_MODIFIERS)}) [+-] )*  # ctrl+, shift-, ...
    (?: f\d{{1,2}}  # a function key
      | arrows? (?:\s+ keys?)?  # "arrow keys", however it is spaced
      | [a-z0-9]  # one letter or digit
      | {alternatives(_KEY_NAMES)}  # a named key, or the glyph a footer draws for it
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)
"""A token that NAMES a key, including the paired glyphs a footer prints whole.

opencode advertises a horizontal select as ``⇆`` and a vertical one as ``↑↓``,
one token each (measured on its dialogs).
"""

_CTRL: Final = ("ctrl+", "ctrl-")

_CTRL_KEY_CHARS: Final = len("ctrl+e")

_MAX_HINTS: Final = 4

_MAX_FOOTER_LINES: Final = 3


_NO_PANE: Final = PaneId("")

_NO_SESSION: Final = SessionId("")


_MAX_BUTTONS: Final = 9

_MAX_BUTTON_CHARS: Final = 40

_MIN_BUTTONS: Final = 2

_BUTTON_TOKENS: Final = re.compile(r"\s{2,}")
"""What separates the tokens of the line a permission dialog draws its buttons on.

Measured on opencode: the labels get three spaces between them (one-space pads
plus its box gap), the hint pairs two.
"""

_HEADER_TOKEN: Final = re.compile(r"[A-Za-z][A-Za-z0-9 '/&+.-]{0,23}")
"""One tab-strip header: a short word or two, nothing that looks like a sentence."""

_MIN_STRIP_TOKENS: Final = 2
_MAX_STRIP_TOKENS: Final = 6

_FOCUS_DISTINCT: Final = 2
"""A focus exists only when the choices carry exactly two backgrounds."""
_FOCUS_MODAL: Final = 2
"""And one of them is carried by at least two choices: the unfocused rest."""
_FOCUS_ODD: Final = 1
"""Leaving exactly one choice on the background the others lack."""
"""The strip's own submit tab, which is an affordance and not a question step."""


class MenuShape(StrEnum):
    """What KIND of screen this is -- a claim kept separate from "is it live"."""

    NONE = "none"
    SINGLE_CHOICE = "single-choice"
    MULTI_SELECT = "multi-select"
    BUTTONS = "buttons"
    """A bordered row of unnumbered choices answered by a moving focus: opencode's
    permission dialog. The arrows are the address, so no row earns a digit."""
    UNKNOWN = "unknown"


class KeyHint(BaseModel):
    """One ``<key> to <verb>`` the screen printed under its options."""

    model_config = ConfigDict(frozen=True)

    key: str
    verb: str

    @property
    def name(self) -> str:
        """``Enter`` -- the KEY alone, for a button that prints its verb beneath it."""
        return _key_label(self.key)

    @property
    def label(self) -> str:
        """``Enter to select`` -- the control's accessible name."""
        return f"{self.name} to {self.verb}"

    @property
    def hands_off(self) -> bool:
        """True when the key gives the terminal to another program: see ``facts.HANDOFF_VERBS``."""
        return _HANDOFF_RE.search(self.verb) is not None


def _ctrl_letter(key: str) -> str | None:
    """The letter a ``ctrl+e`` or ``ctrl-e`` key names, or ``None``."""
    if len(key) == _CTRL_KEY_CHARS and key.startswith(_CTRL) and key[-1] in ascii_lowercase:
        return key[-1]
    return None


def _key_label(key: str) -> str:
    """A key name as a human reads it on a button: ``ctrl-e`` -> ``Ctrl+E``."""
    if (letter := _ctrl_letter(key)) is not None:
        return f"Ctrl+{letter.upper()}"
    return key.capitalize()


def _key_pieces(token: str) -> list[str]:
    """A hint's key-side split into the individual keys it names."""
    pieces = (raw.strip().strip(".") for raw in _KEY_PIECE.split(token.strip().lower()))
    return [piece for piece in pieces if piece]


def _keys_in(token: str, verb: str = "") -> tuple[str, ...]:
    """Every key name a hint's key-side names, or ``()`` if none is recognised."""
    found: list[str] = []
    for raw in _KEY_PIECE.split(token.strip().lower()):
        piece = raw.strip().strip(".")
        if not piece:
            continue
        if (letter := _ctrl_letter(piece)) is not None:
            found.append(f"ctrl-{letter}")
            continue
        if piece == "\u21c6":
            # The glyph names whatever moves between the things the screen shows: on a
            # permission dialog it prints "⇆ select" (the arrows), on a question set
            # "⇆ tab" (the tab key). The verb it printed is the only honest reading.
            found.extend(("tab",) if verb == "tab" else ("left", "right"))
            continue
        found.extend(facts.KEY_ALIASES.get(piece, ()))
    return tuple(dict.fromkeys(found))


def is_key_hint(line: str) -> bool:
    """True when a line is the terminal advertising a KEY, not prose."""
    for segment in _HINT_SPLIT.split(line):
        match = _hint_match(segment.strip(" ()[]"))
        if match is None:
            continue
        pieces = _key_pieces(match.group("keys"))
        if pieces and all(_KEY_TOKEN_RE.fullmatch(piece) for piece in pieces):
            return True
    return False


def _footer_lines(lines: list[str]) -> list[str]:
    """The key-hint footer at the very END of the viewport, and only there.

    Measured on the opencode rig: a blocked pane's read ends inside the dialog,
    whose footer is the bottom line, so the plain walk reaches it. A shell bar
    drawn below would be furniture too -- what it advertises is not the
    dialog's offer, and the corpus is the guard on that line.
    """
    end = len(lines)
    while end and not lines[end - 1].strip():
        end -= 1
    found: list[str] = []
    for line in reversed(lines[:end]):
        if not line.strip() or _DIVIDER_RE.fullmatch(line) or not _is_chrome(line):
            break
        found.append(line)
        if len(found) >= _MAX_FOOTER_LINES:
            break
    return list(reversed(found))


def _without_list_or(part: str) -> str:
    """*part* without the ``or`` that closes a list, as in ``type 1/2/3, or ↑/↓``."""
    rest = part.lstrip()
    if rest[:2].lower() == "or" and rest[2:3].isspace():
        return rest[2:].lstrip()
    return part


def _segments(line: str) -> list[str]:
    """One footer line as hint-sized pieces, commas used only where they must be."""
    pieces: list[str] = []
    for segment in _HINT_SPLIT.split(line):
        whole = _hint_text(segment)
        if _whole_hint(whole) or "," not in whole:
            pieces.append(segment)
            continue
        # "type 1/2/3, or ↑/↓ to select" (hermes): the "or" closes the list, it names no key.
        parts = (_without_list_or(part) for part in whole.split(","))
        pieces.extend(part for part in parts if part.strip())
    return pieces


def _hints(footer: list[str]) -> tuple[KeyHint, ...]:
    """Parse ``<key> to <verb>`` out of the footer lines, in reading order."""
    found: list[KeyHint] = []
    seen: set[tuple[str, str]] = set()
    for hint in (hint for line in footer for hint in _line_hints(line)):
        if (hint.key, hint.verb) not in seen:
            seen.add((hint.key, hint.verb))
            found.append(hint)
    return tuple(found[:_MAX_HINTS])


_HANDOFF_RE: Final = re.compile(rf"\b(?:{alternatives(facts.HANDOFF_VERBS)})\b", re.IGNORECASE)


def _line_hints(line: str) -> list[KeyHint]:
    """Every hint one footer line names, duplicates included."""
    found: list[KeyHint] = []
    for segment in _segments(line):
        if (m := _hint_match(_hint_text(segment))) is None:
            continue
        verb = " ".join(m.group("verb").lower().split())
        if verb in facts.OFFER_REFUSED:
            continue
        found.extend(KeyHint(key=key, verb=verb) for key in _keys_in(m.group("keys"), verb))
    return found


@dataclass(frozen=True, slots=True)
class _Seen:
    """What the shape tests weigh: the screen's lines, its trusted numbered run, its footer hints."""

    lines: list[str]
    run: list[tuple[int, int, str]] | None
    hints: tuple[KeyHint, ...]


def _is_multi_select(seen: _Seen) -> bool:
    """Positive evidence that this screen TOGGLES rather than chooses."""
    lines, run = seen.lines, seen.run
    if any(v in hint.verb for hint in seen.hints for v in facts.TOGGLE_VERBS):
        return True
    if run is not None:
        return any(_CHECKBOX_RE.fullmatch(label) for _, _, label in run)
    start = _dialog_start(lines, len(lines))
    return sum(bool(_CHECKBOX_RE.fullmatch(line)) for line in lines[start:]) >= _MIN_CHECKBOX_ROWS


def _corroborated(seen: _Seen) -> bool:
    """Is the trailing numbered run a LIVE menu?."""
    if seen.run is None:
        return False
    return bool(seen.hints) or _focused(seen.lines, seen.run)


class Stepper(BaseModel):
    """The strip that says this question has more steps behind it."""

    model_config = ConfigDict(frozen=True)

    steps: tuple[str, ...] = ()
    answered: int = 0

    @property
    def total(self) -> int:
        """How many question steps this one belongs to."""
        return len(self.steps)

    @property
    def remaining(self) -> int:
        """How many steps the strip has not drawn ticked yet."""
        return max(0, self.total - self.answered)


def _steps(line: str) -> Stepper | None:
    """The question steps one stepper strip named, or ``None`` if it named none."""
    steps: list[str] = []
    answered = 0
    for token in _STEP_TOKEN_RE.finditer(line):
        box = token.group("box")
        # The strip's own Submit tab is not a question, so it is not a step.
        if box in glyphs.STEP_SUBMIT:
            continue
        steps.append(" ".join(token.group("label").split()))
        answered += box in glyphs.STEP_ANSWERED
    return Stepper(steps=tuple(steps), answered=answered) if steps else None


def _stepper(lines: list[str], stop: int) -> Stepper | None:
    """The nearest stepper strip above ``lines[:stop]``, or ``None``.

    Not bounded by the nearest rule: with no run read, ``stop`` is the screen's
    end, and the last box there is claude's ``Chat about this`` row, under the
    box that asks. The strip's own shape, arrows at both ends around a step
    box, is what keeps transcript text out.
    """
    for index in range(stop - 1, -1, -1):
        if _STEPPER_RE.fullmatch(lines[index].strip()):
            return _steps(lines[index].strip())
    return None


_REQUEST_TITLE_RE: Final = re.compile(r"(?i)MCP server [\"“].+[\"”] requests your input")
"""claude's title over an MCP server's request for input: the same line herdr's
``mcp_elicitation_prompt`` rule calls blocked on."""


def _titled_message(paragraphs: list[str]) -> str:
    """The message under a request title, which is what the form asks, or ``""``."""
    for at, paragraph in enumerate(paragraphs):
        title, *rest = paragraph.split("\n")
        if not _REQUEST_TITLE_RE.fullmatch(title):
            continue
        # Fields below it may carry a "?" of their own; the server's message is the question.
        if rest:
            return "\n".join(rest)
        return paragraphs[at + 1] if at + 1 < len(paragraphs) else ""
    return ""


# A run of digits: what a countdown, a clock or a token counter changes while a question stays put.
_DIGITS_RE: Final = re.compile(r"\d+")


def digits_masked(text: str) -> str:
    """*text* with every run of digits drawn as ``#``, so a ticking count reads as the same."""
    return _DIGITS_RE.sub("#", text)


_COUNTER_RE: Final = re.compile(r"^Question \d+/\d+\b[^\n]*\n")
"""codex's ``Question N/M`` counter, heading the question it counts, whatever that ends with."""

_QUESTION_MARK_RE: Final = re.compile(r"[\w)\]\"'`\u2019\u201d\u2026]\?")
"""A '?' ending a word: a git status's ``[!?]`` in a status line asks nothing."""


def ends_a_question(text: str) -> bool:
    """Whether *text* holds a '?' that ends a word, which is how prose asks."""
    return _QUESTION_MARK_RE.search(text) is not None


def _posed(paragraph: str) -> str:
    """What *paragraph* asks, or ``""``: a counted question, else one ending a word in '?'."""
    if counted := _COUNTER_RE.match(paragraph):
        return paragraph[counted.end() :]
    return paragraph if ends_a_question(paragraph) else ""


def _asks(lines: list[str], stop: int) -> str:
    """A request title's message, else the nearest asking paragraph above *stop*: a dialog, not a box."""
    # A dialog takes the input box's place; with the box drawn, only its status line follows.
    if composer_top(lines) < stop:
        return ""
    start = _dialog_start(lines, stop)
    if start and (message := _titled_message(_askable(lines, start, stop))):
        return message
    # With no rule above there is no box, so only the paragraph right above the run can ask.
    window = _MAX_CHROME_PARAGRAPHS + 1 if start else 1
    for paragraph in reversed(_askable(lines, start, stop)[-window:]):
        if posed := _posed(paragraph):
            return posed
    return ""


def _focused(lines: list[str], run: list[tuple[int, int, str]]) -> bool:
    """Is the terminal cursor parked on one of THESE rows, or on an unnumbered row between them?."""
    if any(_MARKER_RE.match(lines[i]) for i, _, _ in run):
        return True
    return any(_PARKED_RE.match(line) for line in lines[run[0][0] : run[-1][0]])


def _colour_focused(run: list[tuple[int, int, str]], colour: tuple[str, ...]) -> bool:
    """Does the dialog's own colour put the cursor on one of THESE rows?."""
    return _focused_index([_label_bg(colour[i], label) for i, _, label in run]) >= 0


def _rows_are_the_answer(seen: _Seen) -> bool:
    """Does the screen SAY that taking a row answers it, and nothing more?."""
    if seen.run is None:
        return False
    if _asks(seen.lines, seen.run[0][0]):
        return True
    return any(
        hint.key == "enter" and any(verb in hint.verb for verb in facts.SELECT_VERBS)
        for hint in seen.hints
    )


def _shape(seen: _Seen, buttons: tuple[str, ...] = ()) -> MenuShape:
    """Classify the screen, in the order that fails safe."""
    if _is_multi_select(seen):
        return MenuShape.MULTI_SELECT
    if buttons:
        return MenuShape.BUTTONS
    # Single-choice is earned, never defaulted to: failing the test means UNKNOWN.
    if _corroborated(seen) and _rows_are_the_answer(seen):
        return MenuShape.SINGLE_CHOICE
    if seen.hints:
        return MenuShape.UNKNOWN
    return MenuShape.NONE


class PromptOption(BaseModel):
    """One numbered choice offered by a blocked agent."""

    model_config = ConfigDict(frozen=True)

    number: int
    label: str
    checked: bool | None = None

    @property
    def toggles(self) -> bool:
        """True when this row keeps state a keystroke flips rather than answering."""
        return self.checked is not None


def _choices(labels: tuple[str, ...]) -> tuple[PromptOption, ...]:
    """Buttons as options: their position is their number, and no digit is earned."""
    return tuple(PromptOption(number=i + 1, label=label) for i, label in enumerate(labels))


class BlockedPrompt(BaseModel):
    """The question a blocked agent is waiting on, as far as it can be recovered."""

    model_config = ConfigDict(frozen=True)

    target: PaneId = PaneId("")
    session: SessionId = SessionId("")
    focus: int | None = None
    """Which option the terminal's own colour marks as the one in play, or None."""
    question: str = ""
    options: tuple[PromptOption, ...] = ()
    raw: str = ""
    shape: MenuShape = MenuShape.NONE
    corroborated: bool = False
    hints: tuple[KeyHint, ...] = ()
    """The hints a control may be drawn for: hand-offs are kept apart, in ``handoffs``."""
    handoffs: tuple[KeyHint, ...] = ()
    """The hints whose key gives the terminal to another program, so never a control."""
    stepper: Stepper | None = None
    body: str = ""
    asked: bool = False
    digits_move: bool = False
    """True when the rows sit beside a preview, where a digit moves the cursor and answers nothing."""
    drawn_labels: tuple[str, ...] = ()
    """The labels of the trailing row run, named as options or not, with their digits masked."""

    @property
    def drew_rows(self) -> bool:
        """True when the screen ends in a numbered row run, trusted as a menu or not."""
        return bool(self.drawn_labels)

    @field_validator("target", mode="before")
    @classmethod
    def _check_target(cls, value: object) -> PaneId:
        """Refuse an identity where an address belongs (and normalize whitespace)."""
        return as_pane_id(value)

    @field_validator("session", mode="before")
    @classmethod
    def _check_session(cls, value: object) -> SessionId:
        """Refuse an address where an identity belongs. The mirror of the above."""
        return as_session_id(value)

    @property
    def context(self) -> tuple[str, ...]:
        """The dialog's own lines minus the question — what is being asked ABOUT."""
        asked = set(self.question.splitlines())
        return tuple(
            line for line in self.body.splitlines() if line not in asked and not _is_chrome(line)
        )

    @property
    def is_choice(self) -> bool:
        """True when this is a screen we positively recognise as single-choice."""
        return bool(self.options)

    @property
    def wants_colour(self) -> bool:
        """True when a colour read of the same screen could change this parse.

        A refused run the screen's own hints still vouch for may have a cursor
        drawn only as colour, and a buttons row shows its focus only that way.
        """
        return self.shape is MenuShape.BUTTONS or (
            self.drew_rows and bool(self.hints or self.handoffs) and not self.options
        )

    @property
    def answerable(self) -> bool:
        """True when the screen printed keys of its own, beyond the generic keypad."""
        return bool(self.hints or self.handoffs)

    @property
    def fingerprint(self) -> str:
        """A digest of the exact screen this question was read off."""
        return screen_digest(self.raw)

    @property
    def identity(self) -> str:
        """A digest of the QUESTION AS POSED, or ``""`` if nothing was read."""
        # No dialog, no identity: `question` falls back to the status line on a screen with no buttons.
        if not (self.drew_rows or self.shape is not MenuShape.NONE or self.asked):
            return ""
        if not (self.question or self.options or self.shape is MenuShape.MULTI_SELECT):
            return ""
        rows = (f"{o.number}\x1f{o.label}\x1f{o.checked}" for o in self.options)
        # Rows not named as options still answer on Enter, so relabelling one moves the question.
        unnamed = () if self.options else self.drawn_labels
        step = self.stepper
        return screen_digest(
            "\x1e".join(
                (
                    self.shape.value,
                    # Without the body every claude permission prompt is one question.
                    self.body,
                    self.question,
                    "" if step is None else f"{step.answered}/{'\x1d'.join(step.steps)}",
                    *rows,
                    *unnamed,
                )
            )
        )

    @classmethod
    def parse(
        cls,
        text: str,
        *,
        target: PaneId = _NO_PANE,
        session: SessionId = _NO_SESSION,
        ansi: str = "",
    ) -> Self:
        """Split detection output into a question and any numbered options.

        *ansi* is the read *text* was stripped from, when the caller has it:
        the no-cursor gate may then be satisfied by colour on the dialog's own
        lines. Colour from any other read is ignored.
        """
        screen = _prepared(text, ansi)
        lines, barred = screen.lines, screen.barred
        found = _trailing_menu(lines)
        hints = _hints(_footer_lines(lines))
        buttons, buttons_at = _buttons(lines, barred, hints)
        run = _trusted(found, screen)
        seen = _Seen(lines, run, hints)
        shape = _shape(seen, buttons)
        stop = run[0][0] if run is not None else (buttons_at if buttons else len(lines))
        box = range(_box_start(lines, stop, barred), stop)
        parsed = cls(
            target=target,
            session=session,
            question=_heading(lines, box) if shape is MenuShape.BUTTONS else _question(lines, stop),
            options=_choices(buttons) if shape is MenuShape.BUTTONS else _options(run, shape),
            stepper=_stepper(lines, stop) or _tabs_strip(lines, stop, hints),
            body=_body(lines, box),
            drawn_labels=tuple(digits_masked(label) for _, _, label in found or ()),
            asked=bool(_asks(lines, stop)),
            raw=screen.text,
            shape=shape,
            corroborated=_corroborated(seen) or bool(buttons),
            hints=tuple(hint for hint in hints if not hint.hands_off),
            handoffs=tuple(hint for hint in hints if hint.hands_off),
        )
        return _located(parsed, screen, _option_lines(parsed, run, buttons_at))

    @classmethod
    def from_reads(
        cls,
        text: str,
        colour: Callable[[], str | None],
        *,
        target: PaneId = _NO_PANE,
        session: SessionId = _NO_SESSION,
    ) -> Self:
        """Parse a text read, and a colour read of the same screen only when colour could change it.

        *colour* takes that colour read, or answers ``None`` when there is none to take.
        """
        prompt = cls.parse(text, target=target, session=session)
        if not prompt.wants_colour or (ansi := colour()) is None:
            return prompt
        # Text and colour from this one read: beside an older text read, a cursor may sit on a moved row.
        return cls.parse(as_text_read(ansi), target=target, session=session, ansi=ansi)


def _option_lines(
    prompt: BlockedPrompt, run: list[tuple[int, int, str]] | None, buttons_at: int
) -> tuple[int, ...]:
    """The line each of *prompt*'s options was parsed from."""
    if prompt.shape is MenuShape.BUTTONS:
        return (buttons_at,) * len(prompt.options)
    return tuple(i for i, _, _ in run or ())


@dataclass(frozen=True, slots=True)
class _Screen:
    """One read as the grammar sees it, with its colour where colour may count."""

    lines: list[str]
    barred: list[bool]
    text: str
    colour: tuple[str, ...]
    """Per line, the ANSI the same read drew it with -- on the dialog's own lines only."""
    beside: bool = False
    """Whether a preview box was cut away from beside the rows."""


def _prepared(text: str, ansi: str = "") -> _Screen:
    """The screen as the grammar reads it: bars stripped, and where they were.

    A drawn panel is the screen; its rows lost their sides here, so the border
    no longer marks any of them, and no colour is carried into it.
    """
    drawn = text.splitlines()
    rows = _beside_rows(drawn)
    barred = [bool(_EDGE_RE.match(line)) for line in rows]
    # Padded to the pane's width or not, a row reads the same: a blank row of spaces is no rule.
    lines = [_EDGE_RE.sub(r"\g<indent>", line).rstrip() for line in rows]
    if (panel := _panel_screen(lines)) is not None:
        return _Screen(panel, [False] * len(panel), "\n".join(panel), ("",) * len(panel))
    colour = _dialog_colour(text, ansi, barred)
    return _Screen(_reframed(lines), barred, text, colour, beside=rows != drawn)


_SIDE_TOP_RE: Final = re.compile(
    rf"\S\s{{2,}}(?P<corner>{glyphs.one_of(glyphs.PREVIEW_TOP)}){glyphs.one_of(glyphs.LINE_RULE)}"
)
"""A box opening on an option row, right of its label: the preview beside the rows."""

# A tall preview is drawn folded, a tee row across it saying how many lines it hid.
_SIDE_EDGE: Final = glyphs.PREVIEW_SIDE

_SIDE_BOTTOM: Final = glyphs.PREVIEW_BOTTOM


def _side_column(line: str) -> int | None:
    """The column a preview box beside *line*'s option opens at, or ``None``."""
    if OPTION_RE.fullmatch(line) is None or (m := _SIDE_TOP_RE.search(line)) is None:
        return None
    return m.start("corner")


def _side_bottom(rows: list[str], top: int, column: int) -> int | None:
    """The row the box opened at *column* on ``rows[top]`` closes on, or ``None``."""
    for i in range(top + 1, len(rows)):
        mark = rows[i][column : column + 1]
        if mark and mark in _SIDE_BOTTOM:
            return i
        if not mark or mark not in _SIDE_EDGE:
            return None
    return None


def _beside_rows(rows: list[str]) -> list[str]:
    """*rows* with a preview box drawn beside the options, and the column under it, cut away.

    claude draws an option's preview as a box right of the rows when the question
    carries previews, with a ``Notes:`` line under it and its last choice, an
    unnumbered ``Chat about this``, below the next rule. While the note box holds
    the keys, which claude says by adding its editor key to the footer, the rows
    are not what a key reaches, so that screen is left as read.
    """
    at = next((i for i in range(len(rows) - 1, -1, -1) if _side_column(rows[i]) is not None), None)
    if at is None or any(hint.hands_off for hint in _hints(_footer_lines(rows))):
        return rows
    column = _side_column(rows[at]) or 0
    bottom = _side_bottom(rows, at, column)
    if bottom is None:
        return rows
    return _cut_side(rows, range(at, bottom + 1), column)


def _cut_side(rows: list[str], box: range, column: int) -> list[str]:
    """*rows* cut at *column* from *box* down to the next rule, and only chrome kept below it."""
    cut = list(rows)
    below = len(rows)
    for i in range(box.start, len(rows)):
        if _DIVIDER_RE.fullmatch(rows[i]):
            below = i + 1
            break
        if i >= box.stop and rows[i][:column].strip():
            return rows
        cut[i] = rows[i][:column].rstrip()
    # The row under the rule is the dialog's own unnumbered choice, which only the arrows reach.
    for i in range(below, len(rows)):
        if not _is_inert(rows[i]):
            cut[i] = ""
    return cut


def _dialog_colour(text: str, ansi: str, barred: list[bool]) -> tuple[str, ...]:
    """The ANSI of each line of the bordered dialog at the bottom, ``""`` elsewhere.

    Colour is evidence only when it is the same read as the text (every line
    strips to the line the grammar parsed) and only inside the dialog: an
    inline code span in the transcript is drawn on the same background a
    focused row is.
    """
    lines = text.splitlines()
    nothing = ("",) * len(lines)
    drawn = ansi.splitlines()
    stripped = plain(ansi).splitlines()
    if not ansi or len(stripped) < len(lines):
        return nothing
    if any(a.rstrip() != b.rstrip() for a, b in zip(lines, stripped, strict=False)):
        return nothing
    if any(line.strip() for line in stripped[len(lines) :]):
        return nothing
    start = _dialog_block(lines, barred)
    return tuple(drawn[i] if i >= start else "" for i in range(len(lines)))


def _dialog_block(lines: list[str], barred: list[bool]) -> int:
    """Where the bordered block ending the screen starts, or ``len(lines)`` if none does."""
    end = len(lines)
    while end and not lines[end - 1].strip():
        end -= 1
    start = end
    while start and barred[start - 1]:
        start -= 1
    return start if start < end else len(lines)


def _trusted(
    found: list[tuple[int, int, str]] | None, screen: _Screen
) -> list[tuple[int, int, str]] | None:
    """The run the screen has earned: no cursor glyph or colour cursor, no menu."""
    # No cursor, no menu, whatever the footer looks like -- unless the cursor
    # is drawn in colour on the dialog's own rows, in the read the text came from.
    if (
        found is not None
        and not _focused(screen.lines, found)
        and not _colour_focused(found, screen.colour)
    ):
        return None
    return found


def _buttons(
    lines: list[str], barred: list[bool], hints: tuple[KeyHint, ...]
) -> tuple[tuple[str, ...], int]:
    """The unnumbered button labels of a bordered choose-one row, and where they sit.

    opencode's permission dialog draws its choices as one line of buttons under
    the same left border as the rest of the dialog, with the hints it advertises
    on that same line. Both facts are the earning rule: buttons only where the
    border is, and only when the screen itself says the arrows select and enter
    confirms.
    """
    if not _advertises(hints):
        return (), -1
    for i in range(len(lines) - 1, -1, -1):
        if not barred[i]:
            return (), -1
        if not lines[i].strip():
            continue
        labels = _button_labels(lines[i])
        if labels or not _is_chrome(lines[i]):
            return labels, i
    return (), -1


def _hint_shaped(text: str) -> bool:
    """Does *text* have the shape of a hint pair, whatever its verb?

    A permission line mixes buttons and hint pairs; the buttons end where a
    pair begins, and the pair's verb being one we refuse to offer ("ctrl+f
    fullscreen") must not turn the pair into a button.
    """
    for pattern in (_HINT_RE, _VERB_HINT_RE):
        if (m := pattern.fullmatch(_hint_text(text))) is not None:
            pieces = _key_pieces(m.group("keys"))
            return bool(pieces) and all(_KEY_TOKEN_RE.fullmatch(p) for p in pieces)
    return False


def _button_token(token: str) -> str | None:
    """A button label, or ``None`` when the token ends the run or voids the line."""
    if not token or len(token) > _MAX_BUTTON_CHARS:
        return None
    if token[:1].isdigit() or _CHECKBOX_RE.fullmatch(token):
        return None
    return token


def _button_labels(line: str) -> tuple[str, ...]:
    """The leading run of one line that is not hint pairs, when that run is buttons."""
    labels: list[str] = []
    for token in _BUTTON_TOKENS.split(line.strip()):
        if _hint_shaped(token):
            break
        label = _button_token(token)
        if label is None:
            return ()
        labels.append(label)
    return tuple(labels) if _MIN_BUTTONS <= len(labels) <= _MAX_BUTTONS else ()


def _advertises(hints: tuple[KeyHint, ...]) -> bool:
    """Does the screen say the arrows take a choice and enter confirms it?"""
    sideways = any(h.key in ("left", "right") for h in hints)
    enter = any(h.key == "enter" and any(v in h.verb for v in facts.SELECT_VERBS) for h in hints)
    return sideways and enter


_STRIP_WINDOW: Final = 4
"""How far above the options a tabs strip may sit and still be its own."""


# A tabs strip's headers stand three or more spaces apart, wider than the words inside one.
_STRIP_GAP: Final = re.compile(r"\s{3,}")


def _strip_tokens(line: str) -> tuple[str, ...]:
    """A candidate tabs-strip line as its header tokens, or ``()``."""
    tokens = [t.strip() for t in _STRIP_GAP.split(line.strip()) if t.strip()]
    if not _MIN_STRIP_TOKENS <= len(tokens) <= _MAX_STRIP_TOKENS:
        return ()
    if not all(_HEADER_TOKEN.fullmatch(t) for t in tokens):
        return ()
    if tokens[-1].lower() in facts.CONFIRM_TABS:
        tokens = tokens[:-1]
    return tuple(tokens)


def _tabs_strip(lines: list[str], stop: int, hints: tuple[KeyHint, ...]) -> Stepper | None:
    """The line of question tabs above the question, as a stepper, or ``None``.

    opencode draws one tab per question plus its own Confirm affordance, only
    when a set holds more than one question or a choice is multi. The strip
    prints the headers as words, and the footer says tab moves between them;
    both together are the earning rule. Which tab is active is colour, read
    later off the dialog's own lines.
    """
    if not any(h.key == "tab" for h in hints):
        return None
    for i in range(stop - 1, max(0, stop - _STRIP_WINDOW) - 1, -1):
        tokens = _strip_tokens(lines[i])
        if tokens:
            return Stepper(steps=tokens, answered=0)
    return None


def _bg_of(codes: list[str], i: int) -> tuple[str, int]:
    """One SGR code as a background and how many codes it consumed."""
    if i + 1 < len(codes) and codes[i + 1] == "2" and i + 4 < len(codes):
        return f"rgb({','.join(codes[i + 2 : i + 5])})", 4
    if i + 1 < len(codes) and codes[i + 1] == "5" and i + 2 < len(codes):
        return f"c256({codes[i + 2]})", 2
    return "", 0


def _sgr_bg(codes: list[str], bg: str) -> str:
    """The background one SGR sequence leaves in force."""
    i = 0
    while i < len(codes):
        code = codes[i]
        if code in ("", "0", "39", "49"):
            bg = ""
        elif code == "48":
            bg, size = _bg_of(codes, i)
            i += size
        i += 1
    return bg


# Codes are read ";"-separated only, so an SGR with ":" sub-parameters is left as text.
_SGR_CHARS: Final = frozenset("0123456789;")


def _sgr_at(line: str, at: int) -> tuple[int, str] | None:
    """Where the SGR at ``line[at]`` ends and its codes, or ``None`` for any other sequence."""
    escape = escape_at(line, at)
    if escape.sgr is None or not set(escape.sgr) <= _SGR_CHARS:
        return None
    return escape.end, escape.sgr


def _chunks(line: str) -> list[tuple[str, str]]:
    """One ANSI line as ``(background, text)`` chunks, reset splitting them."""
    found: list[tuple[str, str]] = []
    bg = ""
    pos = 0
    at = line.find("\x1b")
    while at >= 0:
        if (sgr := _sgr_at(line, at)) is None:
            # Any other sequence stays in the chunk's text, and an SGR inside it still counts.
            at = line.find("\x1b", at + 1)
            continue
        if line[pos:at]:
            found.append((bg, line[pos:at]))
        pos, codes = sgr
        bg = _sgr_bg(codes.split(";") if codes else ["0"], bg)
        at = line.find("\x1b", pos)
    if line[pos:]:
        found.append((bg, line[pos:]))
    return found


def _label_bg(line: str, label: str) -> str:
    """The background the chunk carrying *label* was drawn on, or ``""``."""
    for bg, text in _chunks(line):
        if label in text:
            return bg
    return ""


def _focused_index(bgs: list[str]) -> int:
    """Which entry is drawn on the background the others lack, or ``-1``.

    The focus is a background the other choices do not carry (measured on
    opencode: theme.warning on the selected button, theme.backgroundElement on
    the active row, the panel background on everything else). Which colour that
    is belongs to the theme, so the rule is relative, never a literal.
    """
    counts = Counter(bgs)
    if len(counts) != _FOCUS_DISTINCT:
        return -1
    modal, _ = counts.most_common(1)[0]
    if counts[modal] < _FOCUS_MODAL:
        return -1
    odd = [i for i, bg in enumerate(bgs) if bg != modal]
    return odd[0] if len(odd) == _FOCUS_ODD else -1


def _located[P: BlockedPrompt](prompt: P, screen: _Screen, rows: tuple[int, ...]) -> P:
    """The same prompt with what the drawing says of its rows: which is focused, what a digit does."""
    if not prompt.options:
        return prompt
    colour = screen.colour
    prompt = _with(prompt, digits_move=screen.beside)
    bgs = [
        _label_bg(colour[at], option.label) for option, at in zip(prompt.options, rows, strict=True)
    ]
    focus = _focused_index(bgs)
    prompt = _with(prompt, focus=focus if focus >= 0 else None)
    if prompt.shape is MenuShape.BUTTONS:
        return prompt
    return _with(prompt, stepper=_answered_strip(prompt, list(colour)))


def _line_with_all(lines: list[str], labels: list[str]) -> int | None:
    """The last line that carries every *label*, which is where a strip lives."""
    for i in range(len(lines) - 1, -1, -1):
        if all(label in lines[i] for label in labels):
            return i
    return None


def _answered_strip(prompt: BlockedPrompt, lines: list[str]) -> Stepper | None:
    """The tabs strip with the active tab read off the dialog's colour, when one was parsed."""
    strip = prompt.stepper
    if strip is None or not strip.steps:
        return strip
    at = _line_with_all(lines, list(strip.steps))
    if at is None:
        return strip
    active = _focused_index([_label_bg(lines[at], step) for step in strip.steps])
    # Every tab before the active one is a question already answered.
    return Stepper(steps=strip.steps, answered=active if active > 0 else 0)


def _with[P: BlockedPrompt](prompt: P, **update: object) -> P:
    """A copy of *prompt* carrying what the ANSI read established."""
    return prompt.model_copy(update=update)


def _options(run: list[tuple[int, int, str]] | None, shape: MenuShape) -> tuple[PromptOption, ...]:
    """The rows of a menu this app is willing to name, with their tick state."""
    if run is None or shape not in (MenuShape.SINGLE_CHOICE, MenuShape.MULTI_SELECT):
        return ()
    rows: list[PromptOption] = []
    for _, number, label in run:
        checked, text = _ticked(label)
        rows.append(PromptOption(number=number, label=text, checked=checked))
    return tuple(rows)


def _is_inert(line: str) -> bool:
    """True for lines that may sit *after* a menu's last option: blanks, rules, hints."""
    return not line.strip() or _is_chrome(line)


def _is_interleave(line: str) -> bool:
    """True for lines that may sit *between* two options of the same menu.

    The cursor's glyph stands where the row's indent was, so parking it on an
    unnumbered row does not turn that row into an interloper.
    """
    return _is_inert(line) or line[:1].isspace() or bool(_PARKED_RE.match(line))


def _opens_with(line: str, corners: str) -> bool:
    """Does *line*, past its indent, open with one of *corners*?"""
    rest = line.lstrip()
    return bool(rest) and rest[0] in corners


_PANEL_TAIL: Final = 6


_TRAILING_SIDE_RE: Final = re.compile(rf"(?P<row>.*?){glyphs.one_of(glyphs.SIDE)}\s*")
"""A row whose opening side the edge-strip already removed, closing side left."""


def _unboxed(line: str) -> str:
    """One panel row with its closing side removed and its own indentation kept.

    The edge-strip already took the opening side, so a bar still leading the
    row is a column rule inside the panel, and what follows it is that
    column's body: a plan's numbered steps beside its outline are not options.
    """
    tail = _TRAILING_SIDE_RE.fullmatch(line)
    # The pad up to the closing side goes too: a padded blank row would read as a rule.
    return tail["row"].rstrip() if tail is not None else line


def _panel_footer(lines: list[str]) -> list[str]:
    """The key hints drawn BELOW a panel, which is where hermes puts them."""
    found: list[str] = []
    for line in lines[:_PANEL_TAIL]:
        if _DIVIDER_RE.fullmatch(line):
            break
        if _is_chrome(line) and line.strip():
            found.append(line)
    return found


def _panel_screen(lines: list[str]) -> list[str] | None:
    """A drawn dialog panel and its footer, as a screen of its own."""
    top = next(
        (i for i in range(len(lines) - 1, -1, -1) if _opens_with(lines[i], glyphs.TOP_LEFT)),
        None,
    )
    if top is None:
        return None
    bottom = next(
        (i for i in range(top + 1, len(lines)) if _opens_with(lines[i], glyphs.BOTTOM_LEFT)),
        None,
    )
    if bottom is None:
        return None
    inside = [_unboxed(line) for line in lines[top + 1 : bottom]]
    if not any(OPTION_RE.fullmatch(line) for line in inside):
        return None
    return _upto_last_option(inside) + _panel_footer(lines[bottom + 1 :])


_BOX_BOTTOM_RE: Final = re.compile(
    rf"\s*{glyphs.one_of(glyphs.BOTTOM_LEFT)}{glyphs.one_of(glyphs.RULE)}*{glyphs.one_of(glyphs.BOTTOM_RIGHT)}"
)

_BOX_TEE_RE: Final = re.compile(
    rf"\s*{glyphs.one_of(glyphs.TEE_LEFT)}{glyphs.one_of(glyphs.RULE)}*{glyphs.one_of(glyphs.TEE_RIGHT)}"
)
"""A rule across a drawn box, which parts its sections as a blank row would."""

_BOX_RULE: Final = glyphs.LINE_RULE[0] * 4


def _closing_box(lines: list[str]) -> range | None:
    """The rows inside a box parted by inner rules that closes the screen, or ``None``."""
    end = len(lines)
    while end and not lines[end - 1].strip():
        end -= 1
    if not end or not _BOX_BOTTOM_RE.fullmatch(lines[end - 1]):
        return None
    top = next((i for i in range(end - 2, -1, -1) if _opens_with(lines[i], glyphs.TOP_LEFT)), None)
    if top is None:
        return None
    inside = range(top + 1, end - 1)
    # Only the shape measured: a box with no inner rule is left as the grammar read it before.
    return inside if any(_BOX_TEE_RE.fullmatch(lines[i]) for i in inside) else None


def _reframed(lines: list[str]) -> list[str]:
    """*lines*, with a box closing the screen redrawn as the rule and gaps the grammar reads.

    omp draws its Ask dialog as a cornered box parted by inner rules; its top
    opens the dialog as claude's rule does, and each inner rule parts a section.
    """
    if (box := _closing_box(lines)) is None:
        return lines
    inside = [
        "" if _BOX_TEE_RE.fullmatch(row) else _unboxed(row) for row in lines[box.start : box.stop]
    ]
    # The bottom stays: a footer is the dialog's own only on the screen's last row.
    return [*lines[: box.start - 1], _BOX_RULE, *inside, *lines[box.stop :]]


def _upto_last_option(inside: list[str]) -> list[str]:
    """A panel's rows, cut after its last option row."""
    last = max(i for i, line in enumerate(inside) if OPTION_RE.fullmatch(line))
    kept = inside[: last + 1]
    return kept + [line for line in inside[last + 1 :] if _is_inert(line)]


def _trailing_menu(lines: list[str]) -> list[tuple[int, int, str]] | None:
    """The ``1..n`` option run anchored at the end of the viewport, or ``None``."""
    matches = [
        (i, int(m.group("number")), m.group("label"))
        for i, line in enumerate(lines)
        if (m := OPTION_RE.fullmatch(line))
    ]
    # A menu is what the agent waits on now, so only blanks, rules and hints may follow it.
    if not matches or not all(_is_inert(line) for line in lines[matches[-1][0] + 1 :]):
        return None
    run = _counted_down(lines, matches)
    if run is None or len(run) < _MIN_MENU_OPTIONS:
        return None
    return list(reversed(run))


def _counted_down(
    lines: list[str], matches: list[tuple[int, int, str]]
) -> list[tuple[int, int, str]] | None:
    """The options from the last one back to option 1, or ``None`` on a gap or an interloper."""
    run: list[tuple[int, int, str]] = []
    expected = matches[-1][1]
    for entry in reversed(matches):
        if entry[1] != expected:
            return None
        if run and not all(_is_interleave(line) for line in lines[entry[0] + 1 : run[-1][0]]):
            return None
        run.append(entry)
        if expected == 1:
            return run
        expected -= 1
    return None


def _paragraphs(lines: list[str], start: int, stop: int) -> list[str]:
    """The non-empty paragraphs of ``lines[start:stop]``, in reading order."""
    found: list[str] = []
    current: list[str] = []
    for raw_line in lines[start:stop]:
        line = "" if _is_chrome(raw_line) else raw_line.strip()
        if line:
            current.append(line)
        elif current:
            found.append("\n".join(current))
            current = []
    if current:
        found.append("\n".join(current))
    return found


def _is_chrome(line: str) -> bool:
    """True for a rule or a key-hint footer: furniture the terminal drew, not text."""
    return bool(_DIVIDER_RE.fullmatch(line)) or _names_key_then_verb(line) or is_key_hint(line)


_MARKS: Final = "\u25cf\u25cb\u2022\u00b7"


def _unmarked(line: str) -> str:
    """*line* without the indent and the bullets a dialog leads its text with."""
    at = 0
    while at < len(line) and (line[at].isspace() or line[at] in _MARKS):
        at += 1
    return line[at:]


def _box_start(lines: list[str], stop: int, barred: list[bool] | None = None) -> int:
    """Where the dialog box begins: the bordered block, else the last rule's end.

    opencode draws the whole dialog inside a left border, so the contiguous
    barred run ending at the options IS the box; a transcript above it is not.
    """
    if barred is not None and stop > 0 and barred[stop - 1]:
        at = stop - 1
        while at > 0 and barred[at - 1]:
            at -= 1
        return at
    return _dialog_start(lines, stop)


def _body(lines: list[str], box: range) -> str:
    """What is being asked ABOUT: the dialog's text above the options."""
    kept = (_unmarked(line).rstrip() for line in lines[box.start : box.stop])
    return "\n".join(line for line in kept if line)


def _dialog_start(lines: list[str], stop: int) -> int:
    """Index just after the last rule before ``stop`` — where the current box begins."""
    for index in range(stop - 1, -1, -1):
        if _DIVIDER_RE.fullmatch(lines[index]):
            return index + 1
    return 0


def _is_option_block(paragraph: str) -> bool:
    """True for a paragraph that is nothing but numbered option rows."""
    rows = paragraph.split("\n")
    return all(OPTION_RE.fullmatch(row) for row in rows)


def _askable(lines: list[str], start: int, stop: int) -> list[str]:
    """The paragraphs of ``lines[start:stop]`` that could be a question at all."""
    return [
        paragraph
        for paragraph in _paragraphs(lines, start, stop)
        if not _is_option_block(paragraph)
    ]


def _heading(lines: list[str], box: range) -> str:
    """The bordered box's own first paragraph: what the dialog calls itself.

    The first paragraph is the heading by construction: the options and their
    descriptions sit below the box, and a permission box never opens with one.
    """
    found = _paragraphs(lines, box.start, box.stop)
    return found[0] if found else ""


def _question(lines: list[str], stop: int) -> str:
    """The paragraph of ``lines[:stop]`` that is being asked."""
    # One resolver, so a screen is never single-choice on a question this then drops.
    if asked := _asks(lines, stop):
        return asked
    fallback = _askable(lines, 0, stop)
    return fallback[-1] if fallback else ""
