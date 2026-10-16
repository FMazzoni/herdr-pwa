"""The bridge to whatever runs the agents.

This module is the engine's contract: every other layer imports from ``herdr_pwa.engine`` and
never from a submodule, so the engine can move a class between its files without breaking a
caller. A name belongs here when something outside the engine uses it, or when it appears in the
signature of a name that does.
"""

from herdr_pwa.engine.cache import (
    CachedSnapshot,
    CachedValue,
    CacheSettings,
    ReadAbandoned,
    SnapshotCache,
)
from herdr_pwa.engine.client import (
    INTERRUPT_KEY,
    NOT_READY_CODE,
    PANE_BUSY_CODE,
    SESSION_MOVED_CODE,
    HerdrClient,
    StartedAgent,
)
from herdr_pwa.engine.config import EngineSettings
from herdr_pwa.engine.dialog import BlockedPrompt, KeyHint, MenuShape, located
from herdr_pwa.engine.model import (
    Agent,
    AgentStatus,
    PaneId,
    Presence,
    SessionId,
    Snapshot,
    Workspace,
    as_pane_id,
    as_session_id,
    rank,
    screen_digest,
)
from herdr_pwa.engine.tail import MAX_TAIL_FILES, Ask, TailCache, chat_window
from herdr_pwa.engine.transcript import (
    HistoryState,
    ModelRef,
    SessionUsage,
    Speaker,
    TranscriptPage,
    TranscriptTurn,
    locate,
)
from herdr_pwa.engine.transport import (
    EngineError,
    EngineMisconfigured,
    EngineTimeout,
    EngineUnavailable,
    EventStream,
    InvalidRequest,
    InvalidResponse,
    ProtocolMismatch,
    RequestTooLarge,
    TargetNotFound,
    Transport,
)
from herdr_pwa.engine.usage import UsageCache

__all__ = [
    "INTERRUPT_KEY",
    "MAX_TAIL_FILES",
    "NOT_READY_CODE",
    "PANE_BUSY_CODE",
    "SESSION_MOVED_CODE",
    "Agent",
    "AgentStatus",
    "Ask",
    "BlockedPrompt",
    "CacheSettings",
    "CachedSnapshot",
    "CachedValue",
    "EngineError",
    "EngineMisconfigured",
    "EngineSettings",
    "EngineTimeout",
    "EngineUnavailable",
    "EventStream",
    "HerdrClient",
    "HistoryState",
    "InvalidRequest",
    "InvalidResponse",
    "KeyHint",
    "MenuShape",
    "ModelRef",
    "PaneId",
    "Presence",
    "ProtocolMismatch",
    "ReadAbandoned",
    "RequestTooLarge",
    "SessionId",
    "SessionUsage",
    "Snapshot",
    "SnapshotCache",
    "Speaker",
    "StartedAgent",
    "TailCache",
    "TargetNotFound",
    "TranscriptPage",
    "TranscriptTurn",
    "Transport",
    "UsageCache",
    "Workspace",
    "as_pane_id",
    "as_session_id",
    "chat_window",
    "locate",
    "located",
    "rank",
    "screen_digest",
]
