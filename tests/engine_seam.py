"""The engine's insides that its own tests reach, named in one place.

Everything outside the engine imports ``herdr_pwa.engine`` alone. The engine's unit tests also
pin things no other layer touches: the wire schema, the socket's frame limits, the transcript
readers' clipping, the read-only URI a store is opened with. Those names live here rather than in
the engine's public surface, so the surface stays what the app uses, and this file is the one
place outside the engine that imports its submodules.
"""

from herdr_pwa.engine import cache as cache_module
from herdr_pwa.engine import client as client_module
from herdr_pwa.engine import generated, transcript
from herdr_pwa.engine.client import discovery_subscriptions, method_of, payload_field
from herdr_pwa.engine.dialog import OPTION_RE, is_key_hint
from herdr_pwa.engine.generated import request as wire_request
from herdr_pwa.engine.generated import success_response as wire_response
from herdr_pwa.engine.generated.event import EventKind
from herdr_pwa.engine.hermes import HERMES_DB, HermesAdapter
from herdr_pwa.engine.notices import HIDDEN, PeerMessage, RecordOrigin, TaskNotice, injected
from herdr_pwa.engine.opencode import (
    OPENCODE_ASK,
    OPENCODE_DB,
    OPENCODE_ROOT,
    OPENCODE_STORAGE_ROOT,
    OpencodeAdapter,
    SessionInfo,
)
from herdr_pwa.engine.store import readonly_uri
from herdr_pwa.engine.transcript import (
    MAX_NOTE_CHARS,
    MAX_PROSE_CHARS,
    MAX_RESULT_CHARS,
    ClaudeRecord,
    Window,
    adapter_for,
    claude_record,
    fold_turns,
    history,
    is_message,
    read_page,
    tally_claude,
    usage,
)
from herdr_pwa.engine.transport import (
    BACKOFF_CAP,
    EVENT_SUBSCRIBE,
    MAX_FRAME_BYTES,
    MAX_REQUEST_BYTES,
    MAX_STRAY_FRAMES,
    SocketTransport,
    raise_for_error_frame,
)

# Private to their modules; a test pins each directly because no public call shows it.
copied_from = transcript._copied_from  # pyright: ignore[reportPrivateUsage]
head_records = transcript._head_records  # pyright: ignore[reportPrivateUsage]
head_window = transcript._head_window  # pyright: ignore[reportPrivateUsage]

__all__ = [
    "BACKOFF_CAP",
    "EVENT_SUBSCRIBE",
    "HERMES_DB",
    "HIDDEN",
    "MAX_FRAME_BYTES",
    "MAX_NOTE_CHARS",
    "MAX_PROSE_CHARS",
    "MAX_REQUEST_BYTES",
    "MAX_RESULT_CHARS",
    "MAX_STRAY_FRAMES",
    "OPENCODE_ASK",
    "OPENCODE_DB",
    "OPENCODE_ROOT",
    "OPENCODE_STORAGE_ROOT",
    "OPTION_RE",
    "ClaudeRecord",
    "EventKind",
    "HermesAdapter",
    "OpencodeAdapter",
    "PeerMessage",
    "RecordOrigin",
    "SessionInfo",
    "SocketTransport",
    "TaskNotice",
    "Window",
    "adapter_for",
    "cache_module",
    "claude_record",
    "client_module",
    "copied_from",
    "discovery_subscriptions",
    "fold_turns",
    "generated",
    "head_records",
    "head_window",
    "history",
    "injected",
    "is_key_hint",
    "is_message",
    "method_of",
    "payload_field",
    "raise_for_error_frame",
    "read_page",
    "readonly_uri",
    "tally_claude",
    "usage",
    "wire_request",
    "wire_response",
]
