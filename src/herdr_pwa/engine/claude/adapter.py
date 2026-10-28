"""Claude Code as a transcript adapter: one JSONL per session, paged by byte offset."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import ClassVar, Final, override

from herdr_pwa.engine.claude.holder import claude_holds
from herdr_pwa.engine.claude.lineage import (
    CLAUDE_TRANSCRIPT_ROOT,
    head_records,
    head_window,
    in_root,
)
from herdr_pwa.engine.claude.pages import read_page, read_since
from herdr_pwa.engine.claude.tally import tally_claude
from herdr_pwa.engine.transcript import (
    MAX_HISTORY_BYTES,
    MAX_USAGE_BYTES,
    Harness,
    HistoryState,
    SessionUsage,
    Source,
    StartRecipe,
    TranscriptAdapter,
    TranscriptPage,
    Window,
)
from herdr_pwa.process_start import Process

_ANOTHER_FILE: Final = 1 << 62


class ClaudeAdapter(TranscriptAdapter):
    """Claude Code: one JSONL per session, and the frontier is a BYTE OFFSET."""

    default_roots: ClassVar[tuple[str, ...]] = (CLAUDE_TRANSCRIPT_ROOT,)
    harness: ClassVar[Harness] = Harness(
        "claude",
        StartRecipe("claude", models=("fable", "opus", "sonnet", "haiku")),
        byte_frontier=True,
        # Claude Code writes tokens and no money, so a cost here would be invented.
        prices_itself=False,
        # Measured: a tool_use record is in the log while its call still runs, before any result.
        records_calls=True,
    )

    @override
    def page(self, source: Source, window: Window) -> TranscriptPage:
        """The *window*'s turns or messages, ending at byte ``before``, oldest first."""
        path = self.log(source)
        if path is None:
            return TranscriptPage(state=HistoryState.absent)
        if window.max_bytes is None and window.messages:
            # Counting messages, the budget must reach past inline screenshots to find them.
            window = replace(window, max_bytes=MAX_HISTORY_BYTES)
        if source.expect and source.expect != path.stem:
            # A `before` offset into another file would be clamped, not refused, so drop it.
            page = read_page(path, replace(window, before=None))
            return page.model_copy(update={"reset": True, "moved": True})
        return read_page(path, window)

    @override
    def since(self, source: Source, window: Window) -> TranscriptPage:
        """Whatever has been APPENDED past byte ``after``, oldest first."""
        path = self.log(source)
        if path is None:
            return TranscriptPage(state=HistoryState.absent)
        if source.expect and source.expect != path.stem:
            # A held cursor is a promise about one file; reading it here would splice two threads.
            page = read_since(path, replace(window, after=_ANOTHER_FILE))
            return page.model_copy(update={"moved": True})
        return read_since(path, window)

    @override
    def tally(self, source: Source, *, max_bytes: int = MAX_USAGE_BYTES) -> SessionUsage:
        """What the session has spent, summed over its whole lineage."""
        path = self.log(source)
        if path is None:
            return SessionUsage(state=HistoryState.absent)
        return _tally_lineage(path, max_bytes=max_bytes)

    @override
    def origin(self, source: Source) -> str:
        """The first ``cwd`` the log records: where it began, not where a tool wandered."""
        path = self._log(source, follow=False)
        if path is None:
            return ""
        for record in head_records(head_window(path)):
            cwd = (record.cwd or "").strip()
            if cwd:
                return cwd
        return ""

    @override
    def holds(self, source: Source, process: Process) -> bool:
        """Whether *process* is the one its sessions/<pid>.json says holds the session."""
        return claude_holds(source, process)

    @classmethod
    def log(cls, source: Source) -> Path | None:
        """The file the session's conversation is in now, forks followed, or ``None``."""
        return cls._log(source, follow=True)

    @staticmethod
    def _log(source: Source, *, follow: bool) -> Path | None:
        """The session's log in the first root holding it, contained to THAT root."""
        if not source.session:
            return None
        for base in source.roots:
            found = in_root(base, source.session, follow=follow)
            if found is not None:
                return found
        return None


def _tally_lineage(path: Path, *, max_bytes: int) -> SessionUsage:
    """Sum *path* and every log its thread was copied out of."""
    return tally_claude(path, max_bytes=max_bytes).tally
