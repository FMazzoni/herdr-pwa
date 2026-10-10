"""Whether another agent is already watching an agent, read from its own process environment."""

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

MANAGED_ENV = "HERDR_PWA_MANAGED"
PROC = Path("/proc")
# Anything else, absent included, means unmanaged: the failure that still buzzes.
_TRUE = frozenset({"1", "true", "yes"})


def _one_variable(pid: int, name: str, *, proc: Path) -> str:
    # environ is a page of secrets: only this one key is decoded, and no error text leaves.
    prefix = f"{name}=".encode()
    try:
        raw = (proc / str(pid) / "environ").read_bytes()
    except OSError:
        return ""
    for entry in raw.split(b"\0"):
        if entry.startswith(prefix):
            return entry[len(prefix) :].decode("utf-8", "replace").strip()
    return ""


def carries_mark(pid: int, *, proc: Path = PROC) -> bool:
    """Does this process declare itself managed? Every unreadable answer is False."""
    return _one_variable(pid, MANAGED_ENV, proc=proc).casefold() in _TRUE


class PaneProcesses(Protocol):
    """The one thing this module needs from the runner."""

    def pane_pids(self, pane_id: str) -> tuple[int, ...]: ...


class ManagedHerd:
    """Answers "is another agent watching this pane", cached on the pane's pid tuple."""

    def __init__(self, panes: PaneProcesses, *, proc: Path = PROC) -> None:
        self._panes: PaneProcesses = panes
        self._proc: Path = proc
        self._cache: dict[str, tuple[tuple[int, ...], bool]] = {}

    def is_managed(self, pane_id: str) -> bool:
        """True only when a pid in this pane provably carries the mark."""
        pids = self._pids(pane_id)
        if not pids:
            _ = self._cache.pop(pane_id, None)
            return False
        cached = self._cache.get(pane_id)
        # environ is fixed at exec, and pinning the whole pid tuple defeats a recycled pid.
        if cached is not None and cached[0] == pids:
            return cached[1]
        managed = any(carries_mark(pid, proc=self._proc) for pid in pids)
        self._cache[pane_id] = (pids, managed)
        return managed

    def forget(self, live: Sequence[str]) -> None:
        """Drop panes no longer in the herd so the cache cannot grow forever."""
        for pane_id in set(self._cache) - set(live):
            del self._cache[pane_id]

    def _pids(self, pane_id: str) -> tuple[int, ...]:
        try:
            return self._panes.pane_pids(pane_id)
        except Exception:  # any runner failure reads as unmanaged, which still buzzes
            log.debug("no process info for %s; treating it as unmanaged", pane_id)
            return ()
