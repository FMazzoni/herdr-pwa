"""Whether a process is the Claude Code holding a session, by the record it keeps of itself."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, ValidationError

from herdr_pwa.engine.transcript import Source
from herdr_pwa.process_start import Process, ticks_since_boot

log = logging.getLogger(__name__)

# Claude Code keeps one record per live process beside its projects folder: <pid>.json.
_SESSIONS: Final = "sessions"


class _ClaudeProcess(BaseModel):
    """What Claude Code records about one of its running processes; the rest is not read."""

    model_config = ConfigDict(extra="ignore")

    sessionId: str  # noqa: N815 - Claude Code's own key
    # The process's start, in clock ticks since boot, as proc(5) field 22 gives it.
    procStart: str  # noqa: N815 - Claude Code's own key
    # Which pid namespace the pid is counted in: "...:pid:[<inode>]".
    pidDomain: str  # noqa: N815 - Claude Code's own key


def claude_holds(source: Source, process: Process) -> bool:
    """Whether *process* is the Claude Code holding *source*'s session; False when unsure."""
    return any(_holds(base.parent / _SESSIONS, source.session, process) for base in source.roots)


def _holds(sessions: Path, session: str, process: Process) -> bool:
    pid, proc = process
    try:
        record = _ClaudeProcess.model_validate_json((sessions / f"{pid}.json").read_bytes())
        namespace = str((proc / str(pid) / "ns" / "pid").readlink())
    except (OSError, ValidationError) as exc:
        log.debug("no holder record for pid %s: %s", pid, exc)
        return False
    # A reused pid has another start, and one in another namespace merely shares the number.
    started = ticks_since_boot(pid, proc=proc)
    return (
        record.sessionId == session
        and started is not None
        and record.procStart == str(started)
        and record.pidDomain.endswith(f":{namespace}")
    )
