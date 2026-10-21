"""The engine's insides that its own tests reach, named in one place.

Everything outside the engine imports ``herdr_pwa.engine`` alone. The engine's unit tests also
pin things no other layer touches: the wire schema, the socket's frame limits, the transcript
readers' clipping, the read-only URI a store is opened with. Those names live here rather than in
the engine's public surface, so the surface stays what the app uses, and this file is the one
place outside the engine that imports its submodules.
"""

from herdr_pwa.engine import cache as cache_module
from herdr_pwa.engine import client as client_module
from herdr_pwa.engine import dialog, generated
from herdr_pwa.engine.claude import (
    ClaudeAdapter,
    ClaudeRecord,
    claude_record,
    parse_claude_line,
    read_page,
    tally_claude,
)
from herdr_pwa.engine.claude import lineage as claude_lineage
from herdr_pwa.engine.claude.lineage import copied_from, head_records, head_window
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
    Window,
    adapter_for,
    fold_page,
    fold_turns,
    history,
    is_message,
    usage,
)
from herdr_pwa.engine.transport import (
    BACKOFF_CAP,
    EVENT_SUBSCRIBE,
    MAX_FRAME_BYTES,
    MAX_REQUEST_BYTES,
    MAX_STRAY_FRAMES,
    SocketTransport,
    decode_frame,
    raise_for_error_frame,
)

# Private to their modules; a test pins each directly because no public call shows it.
HEAD_BYTES = claude_lineage._HEAD_BYTES  # pyright: ignore[reportPrivateUsage] - the internal is what this test pins
HEAD_FINISH = claude_lineage._HEAD_FINISH  # pyright: ignore[reportPrivateUsage] - the internal is what this test pins
dialog_chunks = dialog._chunks  # pyright: ignore[reportPrivateUsage] - the internal is what this test pins

__all__ = [
    "BACKOFF_CAP",
    "EVENT_SUBSCRIBE",
    "HEAD_BYTES",
    "HEAD_FINISH",
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
    "ClaudeAdapter",
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
    "decode_frame",
    "dialog_chunks",
    "discovery_subscriptions",
    "fold_page",
    "fold_turns",
    "generated",
    "head_records",
    "head_window",
    "history",
    "injected",
    "is_key_hint",
    "is_message",
    "method_of",
    "parse_claude_line",
    "payload_field",
    "raise_for_error_frame",
    "read_page",
    "readonly_uri",
    "tally_claude",
    "usage",
    "wire_request",
    "wire_response",
]
