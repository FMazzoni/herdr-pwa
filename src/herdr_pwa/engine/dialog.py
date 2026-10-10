"""Reading a blocked agent's screen: the question, its options, its key hints."""

import re
from enum import StrEnum
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, field_validator

from herdr_pwa.engine.model import PaneId, SessionId, as_pane_id, as_session_id, screen_digest

OPTION_RE = re.compile(r"^\s*[>\u276f▸▶‣*\-•]?\s*(\d{1,2})\s*[.):]\s+(?P<label>\S.*?)\s*$")

_DIVIDER_RE = re.compile(r"^[\s─━╌╍┄┅┈┉═—=_·•\-]{4,}$")

_FOOTER_RE = re.compile(
    r"(?i)(?:\benter\b|\besc\b|\btab\b|\bctrl\b|↑|↓|←|→|arrow)"
    r".*(?:select|cancel|navigate|confirm|choose|toggle|continue|submit|expand|manage)"
)

_MAX_CHROME_PARAGRAPHS = 3

_MIN_MENU_OPTIONS = 2

_MIN_CHECKBOX_ROWS = 2


_TICKED_BOX = "xX*✔✓"

_BLANK_BOX = " ·"

_TICKED_GLYPH = "☑☒◼"

_BLANK_GLYPH = "☐◻"

_CHECKBOX_RE = re.compile(
    rf"^\s*(?:\[(?P<box>[{_TICKED_BOX}{_BLANK_BOX}]?)\]"
    rf"|(?P<glyph>[{_TICKED_GLYPH}{_BLANK_GLYPH}]))"
    r"\s*(?P<rest>\S.*?)\s*$"
)


def _ticked(label: str) -> tuple[bool | None, str]:
    """Split ``"[✔] autumn"`` into the tick state and the label the terminal drew."""
    match = _CHECKBOX_RE.match(label)
    if match is None:
        return None, label
    mark = match.group("box") or match.group("glyph") or ""
    return bool(mark) and mark in _TICKED_BOX + _TICKED_GLYPH, match.group("rest")


_SELECT_VERBS = (
    "select",
    "choose",
    "pick",
    "confirm",
    "proceed",
    "approve",
    "lock",
)

_TOGGLE_VERBS = ("toggle", "check", "uncheck", "deselect", "select all")

_MARKER_RE = re.compile(r"^\s*[>\u276f▸▶‣]\s")

_STEP_TOKEN_RE = re.compile(
    r"(?P<box>[\u2610\u2612\u2611\u2714\u2713])\s*(?P<label>[^\u2610\u2612\u2611\u2714\u2713\u2190\u2192]*)"
)

_STEP_TICKED = "\u2612\u2611"

_STEP_SUBMIT = "\u2714\u2713"

_STEPPER_RE = re.compile(
    r"^\u2190[^\u2190\u2192]*[\u2610\u2612\u2611\u2714\u2713][^\u2190\u2192]*\u2192$"
)


_HINT_RE = re.compile(
    r"^\s*(?P<keys>[^\s].{0,29}?)\s+to\s+(?P<verb>[A-Za-z][A-Za-z0-9 /+-]{0,28}?)\s*$"
)

_HINT_SPLIT = re.compile(r"\s*[·•|\uff5c]\s*|\s{3,}")

_TRAILING_PAREN_RE = re.compile(r"(?<=\S)\s*\([^()]*\)\s*$")


def _hint_text(segment: str) -> str:
    """One footer piece with its wrapping and any trailing countdown removed."""
    return _TRAILING_PAREN_RE.sub("", segment.strip()).strip(" ()[]")


def _whole_hint(text: str) -> bool:
    """Does *text* parse as ONE hint whose keys are all really keys?"""
    match = _HINT_RE.match(text)
    if match is None:
        return False
    pieces = _key_pieces(match.group("keys"))
    return bool(pieces) and all(_KEY_TOKEN_RE.match(piece) for piece in pieces)


_KEY_PIECE = re.compile(r"\s*/\s*|\s+or\s+|\s*,\s*")

_KEY_TOKEN_RE = re.compile(
    r"(?i)^(?:(?:ctrl|shift|alt|opt|option|cmd|meta|super)[+-])*"
    r"(?:f\d{1,2}|enter|return|esc|escape|tab|space|spacebar|backspace|del|delete"
    r"|up|down|left|right|arrows?(?:\s+keys?)?|pgup|pgdn|home|end"
    r"|[a-z0-9]|[\u2191\u2193\u2190\u2192\u21b5\u23ce])$"
)

_KEY_ALIASES: dict[str, tuple[str, ...]] = {
    "enter": ("enter",),
    "return": ("enter",),
    "↵": ("enter",),
    "⏎": ("enter",),
    "esc": ("esc",),
    "escape": ("esc",),
    "tab": ("tab",),
    "space": ("space",),
    "spacebar": ("space",),
    "up": ("up",),
    "down": ("down",),
    "left": ("left",),
    "right": ("right",),
    "↑": ("up",),
    "↓": ("down",),
    "←": ("left",),
    "→": ("right",),
    # "Arrow keys" means the vertical pair; left/right only count where the screen draws them.
    "arrow": ("up", "down"),
    "arrows": ("up", "down"),
    "arrow keys": ("up", "down"),
    "arrow key": ("up", "down"),
}

_CTRL_RE = re.compile(r"^ctrl[+-]([a-z])$")

_MAX_HINTS = 4

_MAX_FOOTER_LINES = 3


_NO_PANE: Final = PaneId("")

_NO_SESSION: Final = SessionId("")


class MenuShape(StrEnum):
    """What KIND of screen this is -- a claim kept separate from "is it live"."""

    NONE = "none"
    SINGLE_CHOICE = "single-choice"
    MULTI_SELECT = "multi-select"
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


def _key_label(key: str) -> str:
    """A key name as a human reads it on a button: ``ctrl-e`` -> ``Ctrl+E``."""
    if (m := _CTRL_RE.match(key)) is not None:
        return f"Ctrl+{m.group(1).upper()}"
    return key.capitalize()


def _key_pieces(token: str) -> list[str]:
    """A hint's key-side split into the individual keys it names."""
    pieces = (raw.strip().strip(".") for raw in _KEY_PIECE.split(token.strip().lower()))
    return [piece for piece in pieces if piece]


def _keys_in(token: str) -> tuple[str, ...]:
    """Every key name a hint's key-side names, or ``()`` if none is recognised."""
    found: list[str] = []
    for raw in _KEY_PIECE.split(token.strip().lower()):
        piece = raw.strip().strip(".")
        if not piece:
            continue
        if (ctrl := _CTRL_RE.match(piece)) is not None:
            found.append(f"ctrl-{ctrl.group(1)}")
            continue
        found.extend(_KEY_ALIASES.get(piece, ()))
    return tuple(dict.fromkeys(found))


def is_key_hint(line: str) -> bool:
    """True when a line is the terminal advertising a KEY, not prose."""
    for segment in _HINT_SPLIT.split(line):
        match = _HINT_RE.match(segment.strip(" ()[]"))
        if match is None:
            continue
        pieces = _key_pieces(match.group("keys"))
        if pieces and all(_KEY_TOKEN_RE.match(piece) for piece in pieces):
            return True
    return False


def _footer_lines(lines: list[str]) -> list[str]:
    """The key-hint footer at the very END of the viewport, and only there."""
    end = len(lines)
    while end and not lines[end - 1].strip():
        end -= 1
    found: list[str] = []
    for line in reversed(lines[:end]):
        if not line.strip() or _DIVIDER_RE.match(line) or not _is_chrome(line):
            break
        found.append(line)
        if len(found) >= _MAX_FOOTER_LINES:
            break
    return list(reversed(found))


def _segments(line: str) -> list[str]:
    """One footer line as hint-sized pieces, commas used only where they must be."""
    pieces: list[str] = []
    for segment in _HINT_SPLIT.split(line):
        whole = _hint_text(segment)
        if _whole_hint(whole) or "," not in whole:
            pieces.append(segment)
            continue
        pieces.extend(part for part in whole.split(",") if part.strip())
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


def _line_hints(line: str) -> list[KeyHint]:
    """Every hint one footer line names, duplicates included."""
    found: list[KeyHint] = []
    for segment in _segments(line):
        if (m := _HINT_RE.match(_hint_text(segment))) is None:
            continue
        verb = " ".join(m.group("verb").lower().split())
        found.extend(KeyHint(key=key, verb=verb) for key in _keys_in(m.group("keys")))
    return found


def _is_multi_select(
    lines: list[str],
    run: list[tuple[int, int, str]] | None,
    hints: tuple[KeyHint, ...],
) -> bool:
    """Positive evidence that this screen TOGGLES rather than chooses."""
    if any(v in hint.verb for hint in hints for v in _TOGGLE_VERBS):
        return True
    if run is not None:
        return any(_CHECKBOX_RE.match(label) for _, _, label in run)
    start = _dialog_start(lines, len(lines))
    return sum(bool(_CHECKBOX_RE.match(line)) for line in lines[start:]) >= _MIN_CHECKBOX_ROWS


def _corroborated(
    lines: list[str],
    run: list[tuple[int, int, str]],
    hints: tuple[KeyHint, ...],
) -> bool:
    """Is the trailing numbered run a LIVE menu?."""
    return bool(hints) or any(_MARKER_RE.match(lines[i]) for i, _, _ in run)


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
        if box in _STEP_SUBMIT:
            continue
        steps.append(" ".join(token.group("label").split()))
        answered += box in _STEP_TICKED
    return Stepper(steps=tuple(steps), answered=answered) if steps else None


def _stepper(lines: list[str], stop: int) -> Stepper | None:
    """The stepper strip above the dialog in ``lines[:stop]``, or ``None``."""
    for index in range(stop - 1, _dialog_start(lines, stop) - 1, -1):
        if _STEPPER_RE.match(lines[index].strip()):
            return _steps(lines[index].strip())
    return None


def _asks(lines: list[str], stop: int) -> str:
    """The question the DIALOG BOX asks, or ``""`` when it asks none."""
    start = _dialog_start(lines, stop)
    # With no rule above there is no box, so only the paragraph right above the run can ask.
    window = _MAX_CHROME_PARAGRAPHS + 1 if start else 1
    for paragraph in reversed(_askable(lines, start, stop)[-window:]):
        if "?" in paragraph:
            return paragraph
    return ""


def _focused(lines: list[str], run: list[tuple[int, int, str]]) -> bool:
    """Is the terminal cursor parked on one of THESE rows?."""
    return any(_MARKER_RE.match(lines[i]) for i, _, _ in run)


def _rows_are_the_answer(
    lines: list[str], run: list[tuple[int, int, str]], hints: tuple[KeyHint, ...]
) -> bool:
    """Does the screen SAY that taking a row answers it, and nothing more?."""
    if _asks(lines, run[0][0]):
        return True
    return any(
        hint.key == "enter" and any(verb in hint.verb for verb in _SELECT_VERBS) for hint in hints
    )


def _shape(
    lines: list[str],
    run: list[tuple[int, int, str]] | None,
    hints: tuple[KeyHint, ...],
    *,
    corroborated: bool,
) -> MenuShape:
    """Classify the screen, in the order that fails safe."""
    if _is_multi_select(lines, run, hints):
        return MenuShape.MULTI_SELECT
    # Single-choice is earned, never defaulted to: failing the test means UNKNOWN.
    if run is not None and corroborated and _rows_are_the_answer(lines, run, hints):
        return MenuShape.SINGLE_CHOICE
    if hints:
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


class BlockedPrompt(BaseModel):
    """The question a blocked agent is waiting on, as far as it can be recovered."""

    model_config = ConfigDict(frozen=True)

    target: PaneId = PaneId("")
    session: SessionId = SessionId("")
    question: str = ""
    options: tuple[PromptOption, ...] = ()
    raw: str = ""
    shape: MenuShape = MenuShape.NONE
    corroborated: bool = False
    hints: tuple[KeyHint, ...] = ()
    stepper: Stepper | None = None
    body: str = ""
    drew_rows: bool = False
    asked: bool = False

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
    def answerable(self) -> bool:
        """True when there is anything to answer with beyond the generic keypad."""
        return bool(self.hints)

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
    ) -> Self:
        """Split detection output into a question and any numbered options."""
        lines = text.splitlines()
        # A drawn panel is the screen; `text` follows it so `raw` is still what was read.
        if (panel := _panel_screen(lines)) is not None:
            lines, text = panel, "\n".join(panel)
        # `found` is the run as read; `run` is the run as trusted.
        found = _trailing_menu(lines)
        run = found
        hints = _hints(_footer_lines(lines))
        # No cursor, no menu, whatever the footer looks like.
        if run is not None and not _focused(lines, run):
            run = None
        corroborated = run is not None and _corroborated(lines, run, hints)
        shape = _shape(lines, run, hints, corroborated=corroborated)
        return cls(
            target=target,
            session=session,
            question=_question(lines, len(lines) if run is None else run[0][0]),
            options=_options(run, shape),
            stepper=_stepper(lines, len(lines) if run is None else run[0][0]),
            body=_body(lines, len(lines) if run is None else run[0][0]),
            drew_rows=found is not None,
            asked=bool(_asks(lines, len(lines) if run is None else run[0][0])),
            raw=text,
            shape=shape,
            corroborated=corroborated,
            hints=hints,
        )


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
    """True for lines that may sit *between* two options of the same menu."""
    return _is_inert(line) or line[:1].isspace()


_PANEL_TOP_RE = re.compile(r"^\s*[╭┌╔]")

_PANEL_BOTTOM_RE = re.compile(r"^\s*[╰└╚]")

_PANEL_SIDE_RE = re.compile(r"^(\s*)[│┃║](.*?)[│┃║]?\s*$")

_PANEL_TAIL = 6


def _unboxed(line: str) -> str:
    """One panel row with its sides removed and its own indentation kept."""
    match = _PANEL_SIDE_RE.match(line)
    return f"{match.group(1)}{match.group(2)}" if match else line


def _panel_footer(lines: list[str]) -> list[str]:
    """The key hints drawn BELOW a panel, which is where hermes puts them."""
    found: list[str] = []
    for line in lines[:_PANEL_TAIL]:
        if _DIVIDER_RE.match(line):
            break
        if _is_chrome(line) and line.strip():
            found.append(line)
    return found


def _panel_screen(lines: list[str]) -> list[str] | None:
    """A drawn dialog panel and its footer, as a screen of its own."""
    top = next(
        (i for i in range(len(lines) - 1, -1, -1) if _PANEL_TOP_RE.match(lines[i])),
        None,
    )
    if top is None:
        return None
    bottom = next(
        (i for i in range(top + 1, len(lines)) if _PANEL_BOTTOM_RE.match(lines[i])),
        None,
    )
    if bottom is None:
        return None
    inside = [_unboxed(line) for line in lines[top + 1 : bottom]]
    if not any(OPTION_RE.match(line) for line in inside):
        return None
    return _upto_last_option(inside) + _panel_footer(lines[bottom + 1 :])


def _upto_last_option(inside: list[str]) -> list[str]:
    """A panel's rows, cut after its last option row."""
    last = max(i for i, line in enumerate(inside) if OPTION_RE.match(line))
    kept = inside[: last + 1]
    return kept + [line for line in inside[last + 1 :] if _is_inert(line)]


def _trailing_menu(lines: list[str]) -> list[tuple[int, int, str]] | None:
    """The ``1..n`` option run anchored at the end of the viewport, or ``None``."""
    matches = [
        (i, int(m.group(1)), m.group("label"))
        for i, line in enumerate(lines)
        if (m := OPTION_RE.match(line))
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
    return bool(_DIVIDER_RE.match(line)) or bool(_FOOTER_RE.search(line)) or is_key_hint(line)


_MARKER_LEAD = re.compile(r"^[\s\u25cf\u25cb\u2022\u00b7]+")


def _body(lines: list[str], stop: int) -> str:
    """What is being asked ABOUT: the dialog's text above the options."""
    start = _dialog_start(lines, stop)
    kept = (_MARKER_LEAD.sub("", line).rstrip() for line in lines[start:stop])
    return "\n".join(line for line in kept if line)


def _dialog_start(lines: list[str], stop: int) -> int:
    """Index just after the last rule before ``stop`` — where the current box begins."""
    for index in range(stop - 1, -1, -1):
        if _DIVIDER_RE.match(lines[index]):
            return index + 1
    return 0


def _is_option_block(paragraph: str) -> bool:
    """True for a paragraph that is nothing but numbered option rows."""
    rows = paragraph.split("\n")
    return all(OPTION_RE.match(row) for row in rows)


def _askable(lines: list[str], start: int, stop: int) -> list[str]:
    """The paragraphs of ``lines[start:stop]`` that could be a question at all."""
    return [
        paragraph
        for paragraph in _paragraphs(lines, start, stop)
        if not _is_option_block(paragraph)
    ]


def _question(lines: list[str], stop: int) -> str:
    """The paragraph of ``lines[:stop]`` that is being asked."""
    # One resolver, so a screen is never single-choice on a question this then drops.
    if asked := _asks(lines, stop):
        return asked
    fallback = _askable(lines, 0, stop)
    return fallback[-1] if fallback else ""
