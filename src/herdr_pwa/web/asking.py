"""How a waiting question is said on the phone, and which of the screen's keys sit open or folded.

All of it is wording and layout (invariant 7): which keys exist, and what each sends, is
``allowed_keys`` read off the screen, and every one of them is drawn here once.
"""

import re
import unicodedata
from dataclasses import dataclass
from typing import Final

from herdr_pwa.decisions import ScreenDecision
from herdr_pwa.engine import BlockedPrompt, KeyHint, MenuShape
from herdr_pwa.writes.keys import allowed_keys, answer_digits, pad_keys

# A context line past these is on the Pane; the question's subject is its first few.
SUBJECT_LINES: Final = 3

# The arrows' own names say what they do, so a screen's "to navigate" adds nothing.
_ARROWS: Final = ("up", "down", "left", "right")
# One order for every pad: the arrows, then Enter, then Esc set apart at the end.
_PAD_ORDER: Final = (*_ARROWS, "enter", "esc")
# A TUI's markdown heading, quote or list mark at the start of a line.
_MARKS: Final = re.compile(r"^(?:#{1,6}|>|[*\u2022\u00b7\u276f\u203a])(?=\s)")


@dataclass(frozen=True, slots=True)
class Key:
    """One key control: the key it sends, its name, and the verb the screen gave it."""

    key: str
    name: str
    verb: str = ""


@dataclass(frozen=True, slots=True)
class Keys:
    """The keys drawn with a question: the pad, the screen's other keys, and whether they fold."""

    pad: tuple[Key, ...]
    extra: tuple[Key, ...]
    folded: bool

    @property
    def names(self) -> str:
        """What the fold holds, said on its closed row."""
        return ", ".join(key.name for key in (*self.extra, *self.pad))


@dataclass(frozen=True, slots=True)
class Wording:
    """A question as the phone says it: a heading in plain words and what it is about."""

    heading: str
    subject: tuple[str, ...]


def plain_line(line: str) -> str:
    """A terminal line without the glyphs and markdown marks a TUI draws before its words."""
    text = line.strip()
    while text:
        mark = _MARKS.match(text)
        if mark is not None:
            text = text[mark.end() :].lstrip()
        elif unicodedata.category(text[0]) in ("So", "Sk"):
            text = text[1:].lstrip()
        else:
            break
    return text


def _lines(text: str | tuple[str, ...]) -> list[str]:
    rows = text.splitlines() if isinstance(text, str) else list(text)
    return [line for line in (plain_line(row) for row in rows) if line]


def wording(prompt: BlockedPrompt) -> Wording:
    """The heading and subject a question is drawn with; the parse itself is left alone."""
    asked = _lines(prompt.question)
    about = _lines(prompt.context)
    # A buttons dialog heads itself "Permission required" over the kind of thing asked about.
    if prompt.shape is MenuShape.BUTTONS and len(asked) > 1:
        asked, about = asked[:1], [*asked[1:], *about]
    heading = "\n".join(asked)
    subject = tuple(line for line in about if line not in asked)
    return Wording(heading or prompt.question, subject[:SUBJECT_LINES])


def _verbs(prompt: BlockedPrompt) -> dict[str, str]:
    said: dict[str, str] = {}
    for hint in prompt.hints:
        said.setdefault(hint.key, hint.verb)
    if prompt.shape is MenuShape.MULTI_SELECT:
        # On a checklist Enter ticks the row the cursor is on, and submits on the last row.
        said["enter"] = "tick"
    return said


def _key(key: str, verbs: dict[str, str]) -> Key:
    verb = "" if key in _ARROWS else verbs.get(key, "")
    return Key(key, KeyHint(key=key, verb=verb).name, verb)


def keys_for(prompt: BlockedPrompt | None, decision: ScreenDecision | None) -> Keys:
    """Every key the screen offers but the rows' digits, worded once, and whether they fold."""
    allowed = allowed_keys(prompt)
    verbs = _verbs(prompt) if prompt is not None else {}
    own = pad_keys(prompt)
    pad = tuple(_key(key, verbs) for key in _PAD_ORDER if key in own and key in allowed)
    # A hint for a pad key is that key's verb, and a digit's hint is its row: neither is drawn twice.
    hinted = dict.fromkeys(
        hint.key
        for hint in (decision.answer_hints if decision is not None else ())
        if hint.key in allowed and hint.key not in own and not hint.key.isdigit()
    )
    extra = tuple(_key(key, verbs) for key in hinted)
    # Rows that answer with a tap make the keys the odd case; rows a cursor answers need them.
    return Keys(pad, extra, folded=bool(answer_digits(prompt)))
