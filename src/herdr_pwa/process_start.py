"""When the process in a pane started, read from /proc, so work a dead process left is not shown."""

import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Final, Protocol

from herdr_pwa.ids import PaneId

log = logging.getLogger(__name__)

# A pane's process changes rarely; a restart is seen within this many seconds.
_TTL: Final = 15.0
# proc(5): starttime is field 22, the 20th after the parenthesised name.
_STARTTIME: Final = 19


class PaneProcesses(Protocol):
    """The one thing this module needs from the runner."""

    def pane_pids(self, pane_id: PaneId) -> tuple[int, ...]: ...


def _boot(proc: Path) -> float | None:
    try:
        lines = (proc / "stat").read_text().splitlines()
    except OSError:
        return None
    found = next((line.split()[1] for line in lines if line.startswith("btime ")), "")
    return float(found) if found.isdigit() else None


def ticks_since_boot(pid: int, *, proc: Path) -> int | None:
    """When *pid* started, in clock ticks since boot; ``None`` when /proc cannot say."""
    try:
        text = (proc / str(pid) / "stat").read_text()
    except OSError:
        return None
    # The name may hold spaces and parentheses; the fields start after its last ")".
    fields = text[text.rfind(")") + 1 :].split()
    value = fields[_STARTTIME] if len(fields) > _STARTTIME else ""
    return int(value) if value.isdigit() else None


def started_at(pid: int, *, proc: Path, ticks: int) -> float | None:
    """When *pid* started, in wall-clock seconds; ``None`` when /proc cannot say."""
    boot = _boot(proc)
    since = ticks_since_boot(pid, proc=proc)
    if boot is None or since is None or ticks <= 0:
        return None
    return boot + since / ticks


class ProcessStarts:
    """When each pane's agent process started: the earliest of its processes after the shell."""

    def __init__(
        self,
        panes: PaneProcesses,
        *,
        proc: Path,
        clock: Callable[[], float],
    ) -> None:
        self._panes: PaneProcesses = panes
        self._proc: Path = proc
        self._clock: Callable[[], float] = clock
        self._ticks: int = os.sysconf("SC_CLK_TCK")
        self._held: dict[PaneId, tuple[float, float | None]] = {}

    def started(self, pane_id: PaneId) -> float | None:
        """When the agent in *pane_id* started; ``None`` when the runner or /proc cannot say."""
        now = self._clock()
        held = self._held.get(pane_id)
        if held is not None and now - held[0] < _TTL:
            return held[1]
        found = self._read(pane_id)
        self._held[pane_id] = (now, found)
        return found

    def _read(self, pane_id: PaneId) -> float | None:
        try:
            pids = self._panes.pane_pids(pane_id)
        except Exception:  # noqa: BLE001 - the engine's error type is a layer up; a failure loses the bound, never the page
            log.debug("no process info for %s", pane_id)
            return None
        # The shell comes first; the agent is the oldest process the pane runs above it.
        starts = [started_at(pid, proc=self._proc, ticks=self._ticks) for pid in pids[1:]]
        known = [start for start in starts if start is not None]
        return min(known) if known else None
