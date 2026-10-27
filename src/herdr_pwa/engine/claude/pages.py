"""Claude Code's log read by the shared byte pager, one record per line."""

from __future__ import annotations

from pathlib import Path

from herdr_pwa.engine.claude.turns import parse_claude_line
from herdr_pwa.engine.pages import read_page as _read_page
from herdr_pwa.engine.pages import read_since as _read_since
from herdr_pwa.engine.transcript import NEWEST, TranscriptPage, TranscriptTurn, Window


def read_page(path: Path, window: Window = NEWEST) -> TranscriptPage:
    """The *window*'s turns or messages, ending at byte ``before``, oldest first."""
    return _read_page(path, window, parse=_claude_turns)


def read_since(path: Path, window: Window) -> TranscriptPage:
    """Whatever has been appended at or past the *window*'s byte ``after``, oldest first."""
    return _read_since(path, window, parse=_claude_turns)


def _claude_turns(_at: int, raw: bytes) -> tuple[TranscriptTurn, ...]:
    """The one turn a Claude Code record holds, or none for a line that is not one."""
    turn = parse_claude_line(raw)
    return () if turn is None else (turn,)
