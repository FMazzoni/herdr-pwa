"""Which stopped agents' conversations end refused for a full context, read once per screen."""

import threading
from collections import OrderedDict
from collections.abc import Iterable
from typing import Final, NamedTuple

from herdr_pwa.decisions import context_stalled, reads_stall
from herdr_pwa.engine import Agent, AgentStatus, Source, TranscriptRoots, Window, history

# Enough turns to see past a run of woken retries to what ended it.
STALL_TURNS: Final = 40
# Every agent of a big herd and then some; past this the least recently seen is forgotten.
MAX_HELD: Final = 256


class _Verdict(NamedTuple):
    revision: int
    status: AgentStatus
    stalled: bool


class Stalls:
    """Per session, whether it stalled, decided again only when its screen or status moves."""

    def __init__(self, roots: TranscriptRoots, *, held: int = MAX_HELD) -> None:
        """Read logs under *roots*, as the Chat tab does."""
        self._roots: TranscriptRoots = roots
        self._max: int = held
        self._held: OrderedDict[str, _Verdict] = OrderedDict()
        self._lock: threading.Lock = threading.Lock()

    def of(self, agents: Iterable[Agent]) -> frozenset[str]:
        """The sessions among *agents* stopped on a full context."""
        return frozenset(a.session for a in agents if a.session and self._stalled(a))

    def _stalled(self, agent: Agent) -> bool:
        # The screen moves with every record a stop writes, so its revision says when to look.
        with self._lock:
            held = self._held.get(agent.session)
            if held is not None and held[:2] == (agent.revision, agent.status):
                self._held.move_to_end(agent.session)
                return held.stalled
            was = held is not None and held.stalled
            stalled = reads_stall(agent.status, held=was) and self._read(agent)
            self._held[agent.session] = _Verdict(agent.revision, agent.status, stalled)
            self._held.move_to_end(agent.session)
            while len(self._held) > self._max:
                _ = self._held.popitem(last=False)
            return stalled

    def _read(self, agent: Agent) -> bool:
        source = Source.of(agent.session, agent_kind=agent.agent, root=self._roots)
        return context_stalled(history(source, Window(limit=STALL_TURNS)).turns)
