"""Terminal pane text -> legible, coloured, phone-sized lines."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Final


# --------------------------------------------------------------------------
# What comes out
# --------------------------------------------------------------------------


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
    """One pane read, split into the three things the phone renders separately."""

    lines: list[Line]
    status: list[list[Run]] = field(default_factory=list)
    draft: list[list[Run]] = field(default_factory=list)
    takeover: bool = False


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


# --------------------------------------------------------------------------
# Escape-sequence grammar. Everything here is a *discard* rule except SGR.
# --------------------------------------------------------------------------

_CSI: Final = re.compile(r"\x1b\[([0-9;:<=>?]*)([ -/]*)([@-~])")

_STRING_SEQ: Final = re.compile(r"\x1b[]PX^_](?:[^\x07\x1b]|\x1b(?!\\))*(?:\x07|\x1b\\|\Z)")

_SHORT_ESC: Final = re.compile(r"\x1b[ -/]*[0-~]?")

_MAX_SGR_PARAMS: Final = 64


# --------------------------------------------------------------------------
# Display width.
# --------------------------------------------------------------------------


_WIDE: Final = frozenset({"W", "F"})

_ZERO: Final = frozenset({"Mn", "Me", "Cf"})


def cells(text: str) -> int:
    """How many terminal columns this text occupies, rather than how many characters it has."""
    total = 0
    for char in text:
        if unicodedata.category(char) in _ZERO:
            continue
        total += 2 if unicodedata.east_asian_width(char) in _WIDE else 1
    return total


# --------------------------------------------------------------------------
# Decoration.
# --------------------------------------------------------------------------

_FRAME_TRIM: Final = "".join(chr(c) for c in range(0x2500, 0x25A0)) + " \t   "

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

_PADDING: re.Pattern[str] = re.compile(f"[{re.escape(_WS)}]{{12,}}")


# --------------------------------------------------------------------------
# Two questions that look like one. The clip guardrail.


_BORDER_GLYPH: Final = "─"

_BORDER_MIN_CELLS: Final = 20

_PROMPT_GLYPHS: Final = "❯>"


def _is_box_border(text: str) -> bool:
    """Is this row the top or bottom border of the agent's own input box?"""
    body = text.strip()
    return len(body) >= _BORDER_MIN_CELLS and body.count(_BORDER_GLYPH) == len(body)


_WIDE_RULE_CHARS: Final = "".join(sorted(c for c in _RULE_LINE_CHARS if not c.isspace()))

_WIDE_RULE_RUN: re.Pattern[str] = re.compile(
    f"[{re.escape(_WIDE_RULE_CHARS)}]{{{_BORDER_MIN_CELLS},}}"
)

_WIDE_RULE_SHARE: Final = 0.75


def _is_wide_rule(text: str) -> bool:
    """Is this row too wide to wrap nicely --'s clip decision, and only that."""
    body = text.strip()
    match = _WIDE_RULE_RUN.search(body)
    if match is None:
        return False
    return cells(match.group()) >= cells(body) * _WIDE_RULE_SHARE


# --------------------------------------------------------------------------
# The agent's own input box, and the draft it was hiding.
# --------------------------------------------------------------------------

_STATUS_MAX_ROWS: Final = 8

_INPUT_MAX_ROWS: Final = 12

_INPUT_PLACEHOLDERS: Final = frozenset(
    {
        "Press up to edit queued messages",
        'Try "how does this work?"',
    }
)

_PASTED_TOKEN: re.Pattern[str] = re.compile(r"\[Pasted text #\d{1,9} \+\d{1,9} lines?\]")


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
        if _is_box_border(plain_text(lines[index])):
            return index
    return -1


def _find_prompt(lines: list[list[Run]], bottom: int) -> int:
    """Index of the prompt line inside the box, or ``-1``."""
    floor = max(-1, bottom - 1 - _INPUT_MAX_ROWS)
    for index in range(bottom - 1, floor, -1):
        text = plain_text(lines[index])
        if _is_box_border(text):
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
    if prompt < 1 or not _is_box_border(plain_text(lines[prompt - 1])):
        return None
    return lines[: prompt - 1], lines[bottom + 1 : end], lines[prompt:bottom]


def _dedent(row: list[Run], gutter: int) -> list[Run]:
    """Drop up to ``gutter`` leading whitespace characters, and never more."""
    text = plain_text(row)
    start = min(gutter, len(text) - len(text.lstrip(_WS)))
    return _slice_runs(row, start, len(text))


def _draft(rows: list[list[Run]]) -> tuple[list[list[Run]], bool]:
    """The stranded draft, and whether Take over is offered --."""
    if not rows:
        return [], False
    head = plain_text(rows[0])
    gutter = len(head) - len(head.lstrip(_WS)) + 1
    gutter += len(head[gutter:]) - len(head[gutter:].lstrip(_WS))
    body = [_rstrip_line(_slice_runs(rows[0], gutter, len(head)))]
    body += [_rstrip_line(_dedent(row, gutter)) for row in rows[1:]]
    while body and not plain_text(body[-1]).strip():
        body.pop()
    flat = "\n".join(plain_text(row) for row in body).strip()
    if not flat or flat in _INPUT_PLACEHOLDERS:
        return [], False
    return body, _PASTED_TOKEN.fullmatch(flat) is None


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


# --------------------------------------------------------------------------
# SGR -> slot
# --------------------------------------------------------------------------


def _slot_for_rgb(red: int, green: int, blue: int) -> int:
    """Quantise one colour onto a slot index by hue, then by lightness."""
    high = max(red, green, blue)
    low = min(red, green, blue)
    chroma = high - low
    if chroma <= _GREY_CHROMA:
        # Achromatic: four steps of grey, so ANSI black lands on something a
        # phone can still see rather than on the background it is printed on.
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


_GREY_CHROMA: Final = 30

_GREY_STEPS: Final = (64, 160, 224)

_GREY_SLOTS: Final = (0, 8, 7, 15)

_HUE_SLOTS: Final = (1, 3, 2, 6, 4, 5)

_BRIGHT_VALUE: Final = 192

_CUBE: Final = (0, 95, 135, 175, 215, 255)


def _slot_for_256(index: int) -> int | None:
    if not 0 <= index <= 255:
        return None
    if index < 16:
        return index
    if index < 232:
        offset = index - 16
        return _slot_for_rgb(_CUBE[offset // 36], _CUBE[(offset // 6) % 6], _CUBE[offset % 6])
    level = 8 + (index - 232) * 10
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
    if mode == 5 and start + 1 < len(params):
        return _slot_for_256(params[start + 1]), 3
    if mode == 2 and start + 3 < len(params):
        red, green, blue = params[start + 1], params[start + 2], params[start + 3]
        if all(0 <= c <= 255 for c in (red, green, blue)):
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
    if 30 <= code <= 37:
        return code - 30
    if 90 <= code <= 97:
        return code - 90 + 8
    return None


def _apply_code(style: _Style, params: list[int], index: int) -> int:
    """Fold one SGR parameter into ``style``; return how many it consumed."""
    code = params[index]
    attribute = _ATTRIBUTE_CODES.get(code)
    if attribute is not None:
        setattr(style, attribute[0], attribute[1])
        return 1
    if code == 0:
        style.reset()
        return 1
    if code == 22:
        style.bold = style.dim = False
        return 1
    if code == 39:
        style.slot = None
        return 1
    if code == 38:
        style.slot, step = _extended_colour(params, index + 1)
        return step
    if code == 48:
        return _extended_colour(params, index + 1)[1]
    slot = _basic_slot(code)
    if slot is not None:
        style.slot = slot
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


# --------------------------------------------------------------------------
# The parse. Before proposing a real terminal emulator here, read this:


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


def _scan(text: str) -> Iterator[tuple[str, str]]:
    """Walk the raw read once, yielding ``text`` / ``sgr`` / ``newline`` events."""
    position = 0
    buffer: list[str] = []
    while position < len(text):
        char = text[position]
        if char not in "\x1b\n":
            if char not in _DROP:
                buffer.append(char)
            position += 1
            continue
        if buffer:
            yield "text", "".join(buffer)
            buffer = []
        if char == "\n":
            yield "newline", ""
            position += 1
            continue
        position, params = _skip_escape(text, position)
        if params is not None:
            yield "sgr", params
    if buffer:
        yield "text", "".join(buffer)


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


# --------------------------------------------------------------------------
# Was this label printed whole, or is it the top half of a wrapped row?
# --------------------------------------------------------------------------


def _screen_width(rows: list[str]) -> int:
    """The widest row in this read, which is the column the terminal wrapped at."""
    return max((cells(row.rstrip()) for row in rows), default=0)


def _label_row(rows: list[str], label: str) -> tuple[int, int]:
    """The last row ending in ``label``, as ``(row index, start column)``."""
    found = (-1, 0)
    for index, row in enumerate(rows):
        body = row.rstrip()
        start = len(body) - len(label)
        if start > 0 and body.endswith(label) and body[start - 1].isspace():
            found = (index, start)
    return found


def _continues(rows: list[str], index: int, column: int, width: int) -> bool:
    """Is ``rows[index + 1]`` the rest of a row the terminal ran out of columns for?"""
    below = rows[index + 1] if index + 1 < len(rows) else ""
    word = below.split()
    if not word or len(below) - len(below.lstrip()) != column:
        return False
    return cells(rows[index].rstrip()) + 1 + cells(word[0]) > width


def printed_whole(screen: str, label: str) -> bool:
    """Did ``screen`` print ``label`` entire, on one row, with nothing continuing it?"""
    rows = screen.splitlines()
    index, column = _label_row(rows, label)
    if index < 0:
        return False
    return not _continues(rows, index, column, _screen_width(rows))


def is_clean_prose(text: str) -> bool:
    """Would :func:`pane_lines` have nothing to strip out of ``text``?"""
    lines = _tokenise(text)
    if not any(plain_text(line).strip() for line in lines):
        return False
    return not any(_is_rule_line(plain_text(line)) for line in lines)


def _strip_decoration(lines: list[list[Run]]) -> list[list[Run]]:
    """'s transform: drop the rules, collapse the frames, keep the text."""
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
    """Turn one raw pane read into what the Pane tab renders --'s split."""
    lines = _tokenise(text)
    split = _split_chrome(lines)
    body, status, prompt = split if split is not None else (lines, [], [])
    body = _strip_decoration(body) if strip else [_rstrip_line(line) for line in body]
    draft, takeover = _draft(prompt)
    return PaneView(
        lines=[
            Line(runs, clip=strip and _is_wide_rule(plain_text(runs)))
            for runs in _trim_blank_edges(body)
        ],
        status=_strip_decoration(status),
        draft=draft,
        takeover=takeover,
    )


def pane_lines(text: str, *, strip: bool = True) -> list[list[Run]]:
    """:func:`pane_view`'s viewport, without the chrome it split off."""
    return [line.runs for line in pane_view(text, strip=strip).lines]
