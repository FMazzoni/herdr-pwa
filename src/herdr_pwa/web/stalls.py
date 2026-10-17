"""Which stopped agents still need you, read once per screen: a full context, or a dialog herdr missed."""

import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable
from typing import Final, NamedTuple

from herdr_pwa.decisions import context_stalled, hides_dialog, reads_stall, screen_asks
from herdr_pwa.engine import (
    Agent,
    AgentStatus,
    BlockedPrompt,
    Source,
    TranscriptRoots,
    Window,
    history,
)

# Enough turns to see past a run of woken retries to what ended it.
STALL_TURNS: Final = 40
# Every agent of a big herd and then some; past this the least recently seen is forgotten.
MAX_HELD: Final = 256

# One agent's screen read for a question, or None when it could not be read.
type ScreenReader = Callable[[Agent], BlockedPrompt | None]


class _Verdict(NamedTuple):
    revision: int
    status: AgentStatus
    found: bool


class _Held:
    """Per session, one verdict, decided again only when its screen or status moves."""

    def __init__(self, held: int) -> None:
        self._max: int = held
        self._held: OrderedDict[str, _Verdict] = OrderedDict()
        self._lock: threading.Lock = threading.Lock()

    def verdict(self, agent: Agent, decide: Callable[[bool], bool]) -> bool:
        # The screen moves with every record a stop writes, so its revision says when to look.
        with self._lock:
            held = self._held.get(agent.session)
            if held is not None and held[:2] == (agent.revision, agent.status):
                self._held.move_to_end(agent.session)
                return held.found
            found = decide(held is not None and held.found)
            self._held[agent.session] = _Verdict(agent.revision, agent.status, found)
            self._held.move_to_end(agent.session)
            while len(self._held) > self._max:
                _ = self._held.popitem(last=False)
            return found


class Stalls:
    """Per session, whether it stalled or still asks, decided again only when its screen moves."""

    def __init__(self, roots: TranscriptRoots, *, held: int = MAX_HELD) -> None:
        """Read logs under *roots*, as the Chat tab does."""
        self._roots: TranscriptRoots = roots
        self._stalls: _Held = _Held(held)
        self._dialogs: _Held = _Held(held)

    def of(self, agents: Iterable[Agent]) -> frozenset[str]:
        """The sessions among *agents* stopped on a full context."""
        return frozenset(a.session for a in agents if a.session and self._stalled(a))

    def asking(self, agents: Iterable[Agent], read: ScreenReader) -> frozenset[str]:
        """The sessions among *agents* herdr calls stopped whose screen still holds a dialog."""
        return frozenset(a.session for a in agents if a.session and self._asks(a, read))

    def _stalled(self, agent: Agent) -> bool:
        return self._stalls.verdict(
            agent, lambda was: reads_stall(agent.status, held=was) and self._read(agent)
        )

    def _asks(self, agent: Agent, read: ScreenReader) -> bool:
        # A question herdr saw is read by its own surfaces, and a running turn is not asking.
        if not hides_dialog(agent.status):
            return False
        return self._dialogs.verdict(agent, lambda _was: _screen_asks(read(agent)))

    def _read(self, agent: Agent) -> bool:
        source = Source.of(agent.session, agent_kind=agent.agent, root=self._roots)
        return context_stalled(history(source, Window(limit=STALL_TURNS)).turns)


def _screen_asks(prompt: BlockedPrompt | None) -> bool:
    return prompt is not None and screen_asks(prompt)
