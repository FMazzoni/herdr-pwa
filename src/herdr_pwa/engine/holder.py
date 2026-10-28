"""Whether the process in a pane holds the session herdr reports for it, as its harness says."""

from __future__ import annotations

from pathlib import Path

from herdr_pwa.engine.model import Agent
from herdr_pwa.engine.transcript import Source, TranscriptRoots, process_holds
from herdr_pwa.process_start import Process


def holds_session(agent: Agent, leader: int | None, *, root: TranscriptRoots, proc: Path) -> bool:
    """Does the pane's foreground process *leader* hold *agent*'s session? False when unsure."""
    source = Source.of(agent.session, agent_kind=agent.agent, root=root)
    if not source.session or leader is None or leader <= 0:
        return False
    return process_holds(source, Process(leader, proc))
