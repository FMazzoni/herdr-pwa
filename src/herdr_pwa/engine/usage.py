"""What each viewed agent has spent, read once and then grown by what its log appended."""

import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from herdr_pwa.engine import transcript
from herdr_pwa.engine.transcript import (
    MAX_USAGE_BYTES,
    HistoryState,
    LiveTally,
    SessionUsage,
    TranscriptRoots,
    grow_claude,
    tally_claude,
)

# Few phones watch few agents; past this the least recently viewed log is forgotten.
MAX_FILES: Final = 6
_SEAL_BYTES: Final = 64


@dataclass(slots=True)
class _Held:
    ident: tuple[int, int]
    live: LiveTally
    # The bytes just before the read end: if they changed, the log was rewritten, not appended to.
    seal: bytes


def _seal(path: Path, end: int) -> bytes:
    start = max(0, end - _SEAL_BYTES)
    with path.open("rb") as handle:
        handle.seek(start)
        return handle.read(end - start)


class UsageCache:
    """Per Claude log, the lineage's tally; a refresh reads only the lines appended since."""

    def __init__(self, *, files: int = MAX_FILES, max_bytes: int = MAX_USAGE_BYTES) -> None:
        self._files: OrderedDict[str, _Held] = OrderedDict()
        self._max_files = files
        self._max_bytes = max_bytes
        self._lock = threading.Lock()

    def read(self, session: str, *, agent_kind: str, root: TranscriptRoots = None) -> SessionUsage:
        """*session*'s tally. Never raises; a harness with no store answers ``unsupported``."""
        if agent_kind.strip().lower() != "claude":
            # opencode and hermes keep a rollup or a table: one query, nothing to grow.
            return transcript.usage(session, agent_kind=agent_kind, root=root)
        path = transcript.locate(session, root=root)
        if path is None:
            return SessionUsage(state=HistoryState.absent)
        try:
            return self._claude(path)
        except OSError:
            self._files.pop(str(path), None)
            return SessionUsage(state=HistoryState.unreadable)

    def _claude(self, path: Path) -> SessionUsage:
        with self._lock:
            stat = path.stat()
            ident = (stat.st_dev, stat.st_ino)
            key = str(path)
            held = self._files.get(key)
            appended = (
                held is not None
                and held.ident == ident
                and stat.st_size >= held.live.end
                and _seal(path, held.live.end) == held.seal
            )
            if held is None or not appended:
                held = _Held(ident, tally_claude(path, max_bytes=self._max_bytes), b"")
            elif stat.st_size > held.live.end:
                grow_claude(held.live, path)
            held.seal = _seal(path, held.live.end)
            self._files[key] = held
            self._files.move_to_end(key)
            while len(self._files) > self._max_files:
                self._files.popitem(last=False)
            return held.live.tally.model_copy(deep=True)
