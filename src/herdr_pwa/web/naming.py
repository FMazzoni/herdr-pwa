"""What a card calls an agent: a title only when it says something the card does not."""

import re
import unicodedata
from typing import Final

from herdr_pwa.decisions import AgentRow, ScreenDecision
from herdr_pwa.engine import MenuShape
from herdr_pwa.panetext import plain

# Spinner and status glyphs a harness prefixes its terminal title with.
_GLYPH_CATEGORIES: Final = frozenset({"So", "Sk"})
# Bullets are punctuation to Unicode, but a harness draws them as a status mark too.
_BULLETS: Final = frozenset("·•∙")
_MAX_GLYPHS: Final = 4
# A harness's placeholder for a session it has not named yet.
_UNNAMED: Final = re.compile(r"^new session\s*-\s*\S+$", re.IGNORECASE)
# A shell's `user@host:~/dir` prompt title names a place, not the work.
_LOCATOR: Final = re.compile(r"^[^\s@:]+@[^\s@:]+(?::\S*)?$")
# How a terminal prompt opens when it asks without a question mark.
_PROMPT_OPENING: Final = re.compile(
    r"^\W*(do you|would you|should i|shall i|allow|approve|proceed|continue|confirm|"
    r"are you sure|press|enter|type|select|choose)\b",
    re.IGNORECASE | re.MULTILINE,
)


def _is_glyph(character: str) -> bool:
    return character in _BULLETS or unicodedata.category(character) in _GLYPH_CATEGORIES


def display_title(title: str) -> str:
    """Strip up to a few leading status glyphs off a terminal title."""
    text = title.lstrip()
    removed = 0
    while text and removed < _MAX_GLYPHS and _is_glyph(text[0]):
        text = text[1:].lstrip()
        removed += 1
    return text.strip() or title.strip()


def _glyphs_only(text: str) -> bool:
    return all(c.isspace() or _is_glyph(c) for c in text)


def page_title(title: str, harness: str, target: str) -> str:
    """What the agent page's top bar calls an agent: its title without the status glyph."""
    return display_title(title) if title.strip() else harness or target


def card_title(row: AgentRow) -> str:
    """The title a feed card prints, or ``""`` when there is none worth printing."""
    text = display_title(plain(row.title))
    if not text or _glyphs_only(text) or _UNNAMED.match(text) or _LOCATOR.match(text):
        return ""
    rejects = {row.harness.strip().casefold(), row.workspace.strip().casefold()} - {""}
    return "" if text.casefold() in rejects else text


def _reads_as_asking(answer: ScreenDecision, question: str) -> bool:
    if answer.asked or answer.options or answer.drew_rows or answer.shape is not MenuShape.NONE:
        return True
    return "?" in question or _PROMPT_OPENING.search(question) is not None


def card_question(row: AgentRow) -> str:
    """The question a feed card prints, or ``""`` when the screen gave nothing readable."""
    if row.answer is None:
        return ""
    question = plain(row.answer.question).strip()
    # A prompt glyph such as a lone "\u276f" is what an unread screen leaves; it says nothing.
    readable = any(unicodedata.category(c)[0] in "LN" for c in question)
    # With nothing asked, the parser's fallback is the screen's last paragraph: scrollback, not a question.
    return question if readable and _reads_as_asking(row.answer, question) else ""


def card_text(text: str) -> str:
    """Terminal text a card prints, with its escape sequences and control bytes dropped."""
    return plain(text)
