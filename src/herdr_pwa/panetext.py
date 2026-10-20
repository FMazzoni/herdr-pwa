"""Terminal pane text -> legible, coloured, phone-sized lines."""

import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Final


@dataclass(frozen=True, slots=True)
class Run:
    """One stretch of pane text that shares one style."""

    text: str
    cls: str = ""


@dataclass(frozen=True, slots=True)
class Line:
    """One rendered pane row: its runs, and whether it is clipped instead of wrapped."""

    runs: list[Run]
    clip: bool = False


@dataclass(frozen=True, slots=True)
class PaneView:
    """One pane read: the viewport, with the agent's input box and statusline split off."""

    lines: list[Line]
    # The rows the harness draws under its input box, in order: a statusline is arbitrary
    # command output and is routinely more than one row. Empty when no box was found.
    status: list[list[Run]] = field(default_factory=list[list[Run]])


MAX_RUNS_PER_LINE: Final = 96

_SLOTS: Final = (
    "black",
    "red",
    "green",
    "yellow",
    "blue",
    "magenta",
    "cyan",
    "white",
    "bright-black",
    "bright-red",
    "bright-green",
    "bright-yellow",
    "bright-blue",
    "bright-magenta",
    "bright-cyan",
    "bright-white",
)

SLOT_CLASSES: Final = tuple(f"ansi-{name}" for name in _SLOTS)

_BOLD: Final = "ansi-b"
_DIM: Final = "ansi-d"
_ITALIC: Final = "ansi-i"
_UNDERLINE: Final = "ansi-u"


# Escape-sequence grammar. Everything here is a discard rule except SGR.

_CSI: Final = re.compile(r"\x1b\[([0-9;:<=>?]*)([ -/]*)([@-~])")
# OSC, DCS, SOS, PM, APC: swallowed whole, so an OSC 8 hyperlink cannot inject a URL.
_STRING_SEQ: Final = re.compile(r"\x1b[]PX^_](?:[^\x07\x1b]|\x1b(?!\\))*(?:\x07|\x1b\\|\Z)")
_SHORT_ESC: Final = re.compile(r"\x1b[ -/]*[0-~]?")
_MAX_SGR_PARAMS: Final = 64


# Display width.

_WIDE: Final = frozenset({"W", "F"})
# Categories that take no cell of their own: they hang off the glyph before.
_ZERO: Final = frozenset({"Mn", "Me", "Cf"})


def cells(text: str) -> int:
    """How many terminal columns this text occupies, rather than how many characters it has."""
    total = 0
    for char in text:
        if unicodedata.category(char) in _ZERO:
            continue
        total += 2 if unicodedata.east_asian_width(char) in _WIDE else 1
    return total


# Decoration.

# Box drawing (U+2500-257F) and block elements (U+2580-259F), plus the spaces around them.
_FRAME_TRIM: Final = "".join(chr(c) for c in range(0x2500, 0x25A0)) + " \t\u00a0\u2000\u2003"
# herdr's two-column welcome box draws its rules inside a line.
_INNER_RULE: Final = re.compile("[─-╿]{3,}")
_RULE_LINE_CHARS: Final = frozenset(_FRAME_TRIM + "-_=~·•")
_RULE_MIN: Final = 4


def _is_rule_line(text: str) -> bool:
    body = text.strip()
    return len(body) >= _RULE_MIN and all(c in _RULE_LINE_CHARS for c in body)


_BOX: str = "".join(chr(c) for c in range(0x2500, 0x25A0))
_WS: str = " \t\u00a0"

_LEAD_FRAME: re.Pattern[str] = re.compile(
    f"^[{re.escape(_WS)}]*(?:[{re.escape(_BOX)}][{re.escape(_WS)}]*)+"
)
_TAIL_FRAME: re.Pattern[str] = re.compile(f"[{re.escape(_WS)}{re.escape(_BOX)}]*$")
# Twelve, not four: four spaces is code indentation behind a diff gutter.
_PADDING: re.Pattern[str] = re.compile(f"[{re.escape(_WS)}]{{12,}}")


# Two questions that look like one: "is this the input box border" and "does this row wrap".

BORDER_GLYPH: Final = "─"
BORDER_MIN_CELLS: Final = 20
_PROMPT_GLYPHS: Final = "\u276f>"


def is_box_border(text: str) -> bool:
    """Is this row the top or bottom border of the agent's own input box?"""
    body = text.strip()
    return len(body) >= BORDER_MIN_CELLS and body.count(BORDER_GLYPH) == len(body)


_WIDE_RULE_CHARS: Final = "".join(sorted(c for c in _RULE_LINE_CHARS if not c.isspace()))
_WIDE_RULE_RUN: re.Pattern[str] = re.compile(
    f"[{re.escape(_WIDE_RULE_CHARS)}]{{{BORDER_MIN_CELLS},}}"
)
_WIDE_RULE_SHARE: Final = 0.75


def _is_wide_rule(text: str) -> bool:
    """Is this row too wide to wrap nicely: the clip decision, and only that."""
    body = text.strip()
    match = _WIDE_RULE_RUN.search(body)
    if match is None:
        return False
    return cells(match.group()) >= cells(body) * _WIDE_RULE_SHARE


# The agent's own input box, and the draft it can hide.

_STATUS_MAX_ROWS: Final = 8
_INPUT_MAX_ROWS: Final = 12


def _prompt_body(text: str) -> str | None:
    """The text after the prompt glyph, or ``None`` if this is not a prompt line."""
    stripped = text.lstrip(_WS)
    if not stripped or stripped[0] not in _PROMPT_GLYPHS:
        return None
    return stripped[1:].strip(_WS)


def _find_bottom_border(lines: list[list[Run]], end: int) -> int:
    """Index of the box's bottom border, searching up from the last non-blank row."""
    floor = max(-1, end - 1 - _STATUS_MAX_ROWS)
    for index in range(end - 1, floor, -1):
        if is_box_border(plain_text(lines[index])):
            return index
    return -1


def _find_prompt(lines: list[list[Run]], bottom: int) -> int:
    """Index of the prompt line inside the box, or ``-1``."""
    floor = max(-1, bottom - 1 - _INPUT_MAX_ROWS)
    for index in range(bottom - 1, floor, -1):
        text = plain_text(lines[index])
        if is_box_border(text):
            return -1
        if _prompt_body(text) is not None:
            return index
    return -1


def _split_chrome(
    lines: list[list[Run]],
) -> tuple[list[list[Run]], list[list[Run]], list[list[Run]]] | None:
    """Peel the agent's input box off the tail. ``None`` when the shape is absent."""
    end = len(lines)
    while end and not plain_text(lines[end - 1]).strip():
        end -= 1
    bottom = _find_bottom_border(lines, end)
    if bottom < 0:
        return None
    prompt = _find_prompt(lines, bottom)
    if prompt < 1 or not is_box_border(plain_text(lines[prompt - 1])):
        return None
    return lines[: prompt - 1], lines[bottom + 1 : end], lines[prompt:bottom]


# A statusline's segments: text between separators (`|`, `│` or `•` standing between
# whitespace), or across the padding a script uses to right-align its last field, or at a
# glyph from a font the phone does not have. The separators stay, as the person printed them.
# The padding is the terminal's width, not a word, so it goes; so does a private-use glyph
# (Nerd Font icons and powerline arrows, U+E000-U+F8FF), which a phone draws as a blank box:
# noise, so the gap between the pieces stands in for it.
_SEGMENT_BREAK: Final = re.compile(
    f"(?P<drop>[{re.escape(_WS)}]{{2,}}|[\ue000-\uf8ff]+)"
    f"|(?:^|(?<=[{re.escape(_WS)}]))[|\u2502\u2022](?:$|(?=[{re.escape(_WS)}]))"
)


def _trimmed(flat: str, start: int, end: int) -> tuple[int, int]:
    """``[start, end)`` of ``flat`` with the whitespace at either edge left out."""
    while start < end and flat[start] in _WS:
        start += 1
    while end > start and flat[end - 1] in _WS:
        end -= 1
    return start, end


def segments(runs: list[Run]) -> list[list[Run]]:
    """One statusline row as the pieces a phone may wrap between, styles kept, nothing reworded."""
    flat = plain_text(runs)
    pieces: list[tuple[int, int]] = []
    at = 0
    for found in _SEGMENT_BREAK.finditer(flat):
        pieces.append(_trimmed(flat, at, found.start()))
        if found.group("drop") is None:
            pieces.append((found.start(), found.end()))
        at = found.end()
    pieces.append(_trimmed(flat, at, len(flat)))
    return [_slice_runs(runs, start, end) for start, end in pieces if end > start]


def composer_top(rows: list[str]) -> int:
    """The row the agent's own input box starts on, or ``len(rows)`` when none is drawn."""
    split = _split_chrome([[Run(row)] for row in rows])
    return len(rows) if split is None else len(split[0])


def _slice_runs(runs: list[Run], start: int, end: int) -> list[Run]:
    """The runs covering ``[start, end)`` of the line's flat text, styles kept."""
    out: list[Run] = []
    offset = 0
    for run in runs:
        length = len(run.text)
        lo, hi = max(start, offset), min(end, offset + length)
        if lo < hi:
            out.append(Run(run.text[lo - offset : hi - offset], run.cls))
        offset += length
    return out


def _strip_frame(runs: list[Run]) -> list[Run]:
    """Collapse one line's box frame and column padding, keeping its text."""
    out = [Run(_INNER_RULE.sub(" ", run.text), run.cls) for run in runs]
    flat = plain_text(out)
    lead = _LEAD_FRAME.match(flat)
    start = lead.end() if lead else 0
    tail = _TAIL_FRAME.search(flat, start)
    end = tail.start() if tail else len(flat)
    out = _slice_runs(out, start, max(start, end))
    return [run for run in _collapse_padding(out) if run.text]


def _collapse_padding(runs: list[Run]) -> list[Run]:
    """Squeeze interior column padding, leaving the line's own indent alone."""
    flat = "".join(run.text for run in runs)
    keep = len(flat) - len(flat.lstrip(_WS))
    out: list[Run] = []
    offset = 0
    for run in runs:
        text = run.text
        if offset < keep:
            edge = min(keep - offset, len(text))
            text = text[:edge] + _PADDING.sub(" ", text[edge:])
        else:
            text = _PADDING.sub(" ", text)
        offset += len(run.text)
        out.append(Run(text, run.cls))
    return out


# SGR -> slot

_GREY_CHROMA: Final = 30
_GREY_STEPS: Final = (64, 160, 224)
_GREY_SLOTS: Final = (0, 8, 7, 15)
_HUE_SLOTS: Final = (1, 3, 2, 6, 4, 5)
_BRIGHT_VALUE: Final = 192
_CUBE: Final = (0, 95, 135, 175, 215, 255)
_CHANNEL_MAX: Final = 255
_CUBE_FIRST: Final = 16
_GREY_RAMP_FIRST: Final = 232

_SGR_RESET: Final = 0
_SGR_NORMAL_INTENSITY: Final = 22
_SGR_FG_EXTENDED: Final = 38
_SGR_FG_DEFAULT: Final = 39
_SGR_BG_EXTENDED: Final = 48
_EXTENDED_256: Final = 5
_EXTENDED_RGB: Final = 2
_BASIC_FG: Final = range(30, 38)
_BRIGHT_FG: Final = range(90, 98)


def _slot_for_rgb(red: int, green: int, blue: int) -> int:
    """Quantise one colour onto a slot index by hue, then by lightness."""
    high = max(red, green, blue)
    low = min(red, green, blue)
    chroma = high - low
    if chroma <= _GREY_CHROMA:
        # Four steps of grey, so ANSI black lands on something a phone can still see.
        for index, ceiling in enumerate(_GREY_STEPS):
            if high < ceiling:
                return _GREY_SLOTS[index]
        return _GREY_SLOTS[-1]
    if high == red:
        hue = 60.0 * (((green - blue) / chroma) % 6.0)
    elif high == green:
        hue = 60.0 * (((blue - red) / chroma) + 2.0)
    else:
        hue = 60.0 * (((red - green) / chroma) + 4.0)
    base = _HUE_SLOTS[int((hue + 30.0) % 360.0 // 60.0)]
    return base + 8 if high >= _BRIGHT_VALUE else base


def _slot_for_256(index: int) -> int | None:
    if not 0 <= index <= _CHANNEL_MAX:
        return None
    if index < _CUBE_FIRST:
        return index
    if index < _GREY_RAMP_FIRST:
        offset = index - _CUBE_FIRST
        return _slot_for_rgb(_CUBE[offset // 36], _CUBE[(offset // 6) % 6], _CUBE[offset % 6])
    level = 8 + (index - _GREY_RAMP_FIRST) * 10
    return _slot_for_rgb(level, level, level)


@dataclass(slots=True)
class _Style:
    """Live SGR state. Only what survives to the output is kept."""

    slot: int | None = None
    bold: bool = False
    dim: bool = False
    italic: bool = False
    underline: bool = False

    def reset(self) -> None:
        self.slot = None
        self.bold = self.dim = self.italic = self.underline = False

    def css(self) -> str:
        parts: list[str] = []
        if self.slot is not None:
            parts.append(SLOT_CLASSES[self.slot])
        if self.bold:
            parts.append(_BOLD)
        if self.dim:
            parts.append(_DIM)
        if self.italic:
            parts.append(_ITALIC)
        if self.underline:
            parts.append(_UNDERLINE)
        return " ".join(parts)


def _extended_colour(params: list[int], start: int) -> tuple[int | None, int]:
    """Read a ``38;5;n`` or ``38;2;r;g;b`` tail. Returns (slot, params consumed)."""
    if start >= len(params):
        return None, 1
    mode = params[start]
    if mode == _EXTENDED_256 and start + 1 < len(params):
        return _slot_for_256(params[start + 1]), 3
    if mode == _EXTENDED_RGB and start + 3 < len(params):
        red, green, blue = params[start + 1], params[start + 2], params[start + 3]
        if all(0 <= c <= _CHANNEL_MAX for c in (red, green, blue)):
            return _slot_for_rgb(red, green, blue), 5
        return None, 5
    return None, len(params) - start + 1


_ATTRIBUTE_CODES: Final[dict[int, tuple[str, bool]]] = {
    1: ("bold", True),
    2: ("dim", True),
    3: ("italic", True),
    4: ("underline", True),
    23: ("italic", False),
    24: ("underline", False),
}


def _basic_slot(code: int) -> int | None:
    """``30-37`` and ``90-97`` are the sixteen slots, spelled two ways."""
    if code in _BASIC_FG:
        return code - _BASIC_FG.start
    if code in _BRIGHT_FG:
        return code - _BRIGHT_FG.start + 8
    return None


def _apply_simple(style: _Style, code: int) -> None:
    """Fold one single-parameter SGR code into ``style``."""
    attribute = _ATTRIBUTE_CODES.get(code)
    if attribute is not None:
        setattr(style, attribute[0], attribute[1])
    elif code == _SGR_RESET:
        style.reset()
    elif code == _SGR_NORMAL_INTENSITY:
        style.bold = style.dim = False
    elif code == _SGR_FG_DEFAULT:
        style.slot = None
    elif (slot := _basic_slot(code)) is not None:
        style.slot = slot


def _apply_code(style: _Style, params: list[int], index: int) -> int:
    """Fold one SGR parameter into ``style``; return how many it consumed."""
    code = params[index]
    if code in (_SGR_FG_EXTENDED, _SGR_BG_EXTENDED):
        # A background is consumed and thrown away, or its 2 would re-read as dim.
        slot, step = _extended_colour(params, index + 1)
        if code == _SGR_FG_EXTENDED:
            style.slot = slot
        return step
    _apply_simple(style, code)
    return 1


def _apply_sgr(style: _Style, raw: str) -> None:
    """Fold one SGR parameter string into ``style``."""
    fields = raw.replace(":", ";").split(";")
    if len(fields) > _MAX_SGR_PARAMS:
        return
    try:
        params = [int(f) if f else 0 for f in fields]
    except ValueError:
        return
    index = 0
    while index < len(params):
        index += _apply_code(style, params, index)


# The parse: a scan that keeps SGR and drops everything else, deliberately not a terminal emulator.

_DROP: Final = frozenset(chr(c) for c in range(0x20) if c not in (0x09, 0x0A)) | frozenset("\x7f")


def _skip_escape(text: str, position: int) -> tuple[int, str | None]:
    """Consume one escape sequence. Returns (next position, SGR params or None)."""
    match = _STRING_SEQ.match(text, position)
    if match is not None:
        return match.end(), None
    match = _CSI.match(text, position)
    if match is not None:
        is_sgr = match.group(3) == "m" and not match.group(2)
        return match.end(), match.group(1) if is_sgr else None
    match = _SHORT_ESC.match(text, position)
    return (match.end() if match else position + 1), None


def _plain_run(text: str, position: int) -> tuple[int, str]:
    """Consume printable text up to the next escape or newline, dropping control bytes."""
    end = position
    while end < len(text) and text[end] not in "\x1b\n":
        end += 1
    return end, "".join(char for char in text[position:end] if char not in _DROP)


def _next_event(text: str, position: int) -> tuple[int, tuple[str, str] | None]:
    """Consume one newline, escape or printable stretch, and the event it makes, if any."""
    char = text[position]
    if char == "\n":
        return position + 1, ("newline", "")
    if char == "\x1b":
        position, params = _skip_escape(text, position)
        return position, None if params is None else ("sgr", params)
    position, chunk = _plain_run(text, position)
    return position, ("text", chunk) if chunk else None


def _scan(text: str) -> Iterator[tuple[str, str]]:
    """Walk the raw read once, yielding ``text`` / ``sgr`` / ``newline`` events."""
    position = 0
    while position < len(text):
        position, event = _next_event(text, position)
        if event is not None:
            yield event


def _append(runs: list[Run], text: str, cls: str) -> None:
    """Add one run, or fold it into the last one once the cap is reached."""
    if len(runs) < MAX_RUNS_PER_LINE:
        runs.append(Run(text, cls))
    elif runs:
        runs[-1] = Run(runs[-1].text + text, runs[-1].cls)


def _tokenise(text: str) -> list[list[Run]]:
    """Split a raw pane read into lines of styled runs, discarding everything else."""
    lines: list[list[Run]] = []
    runs: list[Run] = []
    style = _Style()
    cls = ""
    for kind, value in _scan(text):
        if kind == "text":
            _append(runs, value, cls)
        elif kind == "sgr":
            _apply_sgr(style, value)
            cls = style.css()
        else:
            lines.append(runs)
            runs = []
    if runs:
        lines.append(runs)
    return lines


def _rstrip_line(runs: list[Run]) -> list[Run]:
    """Drop the padding herdr wrote to fill the desktop pane's width."""
    out = list(runs)
    while out:
        text = out[-1].text.rstrip()
        if text:
            out[-1] = Run(text, out[-1].cls)
            break
        out.pop()
    return out


def plain_text(runs: list[Run]) -> str:
    """The text of one line with its styling forgotten."""
    return "".join(run.text for run in runs)


def plain(text: str) -> str:
    """``text`` with every escape sequence and control byte dropped, its lines kept."""
    return "\n".join(plain_text(line) for line in _tokenise(text))


def as_text_read(ansi: str) -> str:
    """A colour read as the text read of the same screen: escapes dropped, padding trimmed."""
    return "\n".join(line.rstrip() for line in plain(ansi).splitlines())


# Was this label printed whole, or is it the top half of a wrapped row?


def _screen_width(rows: list[str]) -> int:
    """The widest row in this read, which is the column the terminal wrapped at."""
    return max((cells(row.rstrip()) for row in rows), default=0)


_BESIDE_BOX_RE = re.compile(r"\s{2,}[┌╭│┃└╰]")
"""The gap and the side of a box drawn right of a row, as a preview beside an option is."""


@dataclass(frozen=True, slots=True)
class _Placed:
    """Where a label sits: its row, its start column, its row's text and the room it had."""

    index: int
    column: int
    text: str
    width: int


def _ends_in(row: str, label: str) -> tuple[str, int] | None:
    """*row* up to where it ends in *label*, and the room a box beside it left, or ``None``."""
    body = row.rstrip()
    if body.endswith(label):
        return body, 0
    at = body.find(label)
    while at >= 0:
        end = at + len(label)
        if (box := _BESIDE_BOX_RE.match(body, end)) is not None:
            # Beside a box, the row's room ends where the box begins.
            return body[:end], cells(body[: box.end() - 1])
        at = body.find(label, at + 1)
    return None


def _label_row(rows: list[str], label: str) -> _Placed | None:
    """The last row ending in ``label``, or in ``label`` beside a drawn box."""
    found = None
    for index, row in enumerate(rows):
        if (ended := _ends_in(row, label)) is None:
            continue
        body, room = ended
        start = len(body) - len(label)
        if start > 0 and body[start - 1].isspace():
            found = _Placed(index, start, body, room)
    return found


def _continues(rows: list[str], placed: _Placed, width: int) -> bool:
    """Is ``rows[index + 1]`` the rest of a row the terminal ran out of columns for?"""
    below = rows[placed.index + 1] if placed.index + 1 < len(rows) else ""
    word = below.split()
    if not word or len(below) - len(below.lstrip()) != placed.column:
        return False
    return cells(placed.text) + 1 + cells(word[0]) > (placed.width or width)


def printed_whole(screen: str, label: str) -> bool:
    """Did ``screen`` print ``label`` entire, on one row, with nothing continuing it?"""
    rows = screen.splitlines()
    placed = _label_row(rows, label)
    if placed is None:
        return False
    return not _continues(rows, placed, _screen_width(rows))


def is_clean_prose(text: str) -> bool:
    """Would :func:`pane_lines` have nothing to strip out of ``text``?"""
    lines = _tokenise(text)
    if not any(plain_text(line).strip() for line in lines):
        return False
    return not any(_is_rule_line(plain_text(line)) for line in lines)


def _strip_decoration(lines: list[list[Run]]) -> list[list[Run]]:
    """Drop the rules, collapse the frames, keep the text."""
    kept: list[list[Run]] = []
    for line in lines:
        if _is_rule_line(plain_text(line)):
            continue
        trimmed = _strip_frame(line)
        if plain_text(trimmed).strip():
            kept.append(trimmed)
        elif not (kept and not plain_text(kept[-1]).strip()):
            # A blank line is structure; two in a row is padding.
            kept.append([])
    return kept


def _trim_blank_edges(lines: list[list[Run]]) -> list[list[Run]]:
    """Drop the blank rows herdr pads the viewport with, top and bottom."""
    out = list(lines)
    while out and not plain_text(out[0]).strip():
        out.pop(0)
    while out and not plain_text(out[-1]).strip():
        out.pop()
    return out


def pane_view(text: str, *, strip: bool = True) -> PaneView:
    """Turn one raw pane read into the viewport, without the agent's input box."""
    lines = _tokenise(text)
    split = _split_chrome(lines)
    # The input box is cut: its text may not be the person's, so it is never drawn as theirs.
    # The statusline under it leaves the viewport too, and the stats strip draws it instead.
    body = split[0] if split is not None else lines
    body = _strip_decoration(body) if strip else [_rstrip_line(line) for line in body]
    status = [] if split is None else [_rstrip_line(line) for line in split[1]]
    return PaneView(
        lines=[
            Line(runs, clip=strip and _is_wide_rule(plain_text(runs)))
            for runs in _trim_blank_edges(body)
        ],
        status=[line for line in status if plain_text(line).strip()],
    )


def pane_lines(text: str, *, strip: bool = True) -> list[list[Run]]:
    """:func:`pane_view`'s viewport, without the chrome it split off."""
    return [line.runs for line in pane_view(text, strip=strip).lines]
