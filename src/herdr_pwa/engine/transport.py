"""The wire: one AF_UNIX connection to the runner, and the error taxonomy."""

import errno
import itertools
import json
import logging
import os
import socket
import threading
import time
import weakref
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from secrets import token_hex
from typing import Any, Final, NamedTuple, Protocol, runtime_checkable

__all__ = [
    "BACKOFF_CAP",
    "EVENT_SUBSCRIBE",
    "MAX_FRAME_BYTES",
    "MAX_REQUEST_BYTES",
    "MAX_STRAY_FRAMES",
    "SUBSCRIPTION_STARTED",
    "EngineError",
    "EngineMisconfigured",
    "EngineTimeout",
    "EngineUnavailable",
    "EventStream",
    "InvalidRequest",
    "InvalidResponse",
    "ProtocolMismatch",
    "RequestTooLarge",
    "SocketTransport",
    "TargetNotFound",
    "Transport",
]

log = logging.getLogger(__name__)

MAX_FRAME_BYTES: Final = 4 * 1024 * 1024
"""Hard cap on one reply while we are still looking for its newline.
"""

MAX_REQUEST_BYTES: Final = 1024 * 1024
"""The largest request line herdr 0.8.0 reads, measured; one more byte and it hangs up.
"""

MAX_STRAY_FRAMES: Final = 8
"""How many frames addressed to somebody else we step over before giving up.
"""

BACKOFF_BASE: Final = 0.1
"""First and largest wait after a failed connect.
"""

BACKOFF_CAP: Final = 2.0
"""The ceiling the doubling backoff is capped at."""

_CHUNK: Final = 65536
"""Bytes per `recv`. One snapshot arrives in a handful of these."""

EVENT_SUBSCRIBE: Final = "events.subscribe"
"""The one method whose reply is not the end of the exchange."""

SUBSCRIPTION_STARTED: Final = "subscription_started"
"""What the runner answers a subscribe with, before any event arrives."""

_EXHAUSTED: Final = frozenset({errno.EMFILE, errno.ENFILE})
"""`socket()` refusing for lack of descriptors: this process or the whole box.
"""


# --------------------------------------------------------------------------
# Fork safety
# --------------------------------------------------------------------------

_LIVE: "weakref.WeakSet[SocketTransport]" = weakref.WeakSet()
"""Every live transport, weakly held so the fork hook keeps no dropped one alive.
"""

_REGISTRY_LOCK: Final = threading.Lock()
"""Guards `_LIVE`, and is held across the fork by the `before` hook."""


def _before_fork() -> None:
    _REGISTRY_LOCK.acquire()


def _after_fork_in_parent() -> None:
    _REGISTRY_LOCK.release()


def _after_fork_in_child() -> None:
    """Re-mint every transport this child inherited, then release the registry."""
    try:
        for wire in list(_LIVE):
            wire._adopt_fork()
    finally:
        _REGISTRY_LOCK.release()


os.register_at_fork(
    before=_before_fork,
    after_in_parent=_after_fork_in_parent,
    after_in_child=_after_fork_in_child,
)


# The error taxonomy — imported and re-exported by client.py, which is where
# the rest of the app has always got it from.


class EngineError(Exception):
    """Any failure talking to the agent runner."""

    code = "engine_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


class EngineMisconfigured(EngineError):
    """This box's configuration will not load, so no client can be built (-> 500)."""

    code = "engine_misconfigured"


class EngineUnavailable(EngineError):
    """The runner is not running, or its socket is stale or unreachable (-> 503)."""

    code = "engine_unavailable"


class EngineTimeout(EngineError):
    """The runner did not answer inside the timeout (-> 503)."""

    code = "engine_timeout"


class InvalidRequest(EngineError, ValueError):
    """The caller asked for something this client cannot send (-> 400)."""

    code = "invalid_request"


class RequestTooLarge(InvalidRequest):
    """Past what the runner will read, so it is refused here and never sent (-> 400)."""

    code = "request_too_large"

    def __init__(self, message: str, *, size: int, limit: int) -> None:
        super().__init__(message)
        self.size = size
        self.limit = limit


class InvalidResponse(EngineError):
    """The runner answered something that is not the JSON contract (-> 502)."""

    code = "invalid_response"


class TargetNotFound(EngineError):
    """The addressed agent vanished mid-flight (-> 404)."""

    code = "target_not_found"


class ProtocolMismatch(EngineError):
    """The runner speaks an unexpected snapshot protocol, so writes fail closed (-> 409)."""

    code = "protocol_mismatch"


_NOT_FOUND_CODES: Final = frozenset(
    {"agent_not_found", "pane_not_found", "target_not_found", "tab_not_found"}
)
"""Error codes the runner reports for "that target is gone"."""

_UNAVAILABLE_CODES: Final = frozenset(
    {"server_not_running", "socket_not_found", "connection_refused"}
)
"""Error codes that mean the runner is not reachable rather than misused."""


def raise_for_error_frame(payload: dict[str, Any]) -> None:
    """Map a peer-reported error frame onto the taxonomy, or return."""
    error = payload.get("error")
    if not isinstance(error, dict):
        return
    code = str(error.get("code") or "engine_error")
    message = str(error.get("message") or code)
    if code in _NOT_FOUND_CODES:
        raise TargetNotFound(message, code=code)
    if code in _UNAVAILABLE_CODES:
        raise EngineUnavailable(message, code=code)
    raise EngineError(message, code=code)


def _why_unreachable(path: Path, exc: OSError) -> str:
    """Say which of the "it isn't there" shapes a failed connect was."""
    if isinstance(exc, FileNotFoundError):
        return (
            f"no runner socket at {path}: the runner is not running, or "
            "this service was told the wrong path"
        )
    if isinstance(exc, ConnectionRefusedError):
        return (
            f"nothing is listening on {path}: the socket file is stale, "
            "left behind by a runner that is gone"
        )
    if isinstance(exc, PermissionError):
        return f"not allowed to connect to {path}: {exc}"
    if exc.errno == errno.ENOTSOCK:
        return f"{path} exists but is not a socket"
    return f"could not connect to the runner at {path}: {exc}"


def _decode(line: bytes) -> dict[str, Any]:
    """Bytes to object, with both ways of not being one mapped."""
    try:
        payload: Any = json.loads(line.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise InvalidResponse(
            f"the runner sent a frame that is not JSON: {line[:200]!r}"
        ) from exc
    if not isinstance(payload, dict):
        raise InvalidResponse(f"expected a JSON object, got {type(payload).__name__}")
    return payload


# --------------------------------------------------------------------------
# The seam
# --------------------------------------------------------------------------


class _Call(NamedTuple):
    """One call's identity and its *one* time budget."""

    method: str
    deadline: float
    budget: float

    def remaining(self, now: float) -> float:
        return self.deadline - now


@runtime_checkable
class Transport(Protocol):
    """What :class:`~herdr_pwa.engine.client.HerdrClient` needs from a wire."""

    def call(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float | None = None,
        idempotent: bool = True,
    ) -> dict[str, Any]:
        """Issue one request and return its parsed reply object."""
        ...

    def close(self) -> None:
        """Release whatever is held open. Calling it twice must be harmless."""
        ...


class SocketTransport:
    """Newline-delimited JSON-RPC over one long-lived AF_UNIX connection."""

    def __init__(
        self,
        path: Path | str,
        *,
        timeout: float,
        max_frame_bytes: int = MAX_FRAME_BYTES,
        backoff_cap: float = BACKOFF_CAP,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.path = Path(path)
        self.timeout = timeout
        self.max_frame_bytes = max_frame_bytes
        self.backoff_cap = backoff_cap
        self._clock = clock
        self._lock = threading.Lock()
        """Held for a whole exchange, so at most one request is on the wire."""
        self._sock: socket.socket | None = None
        self._buffer = b""
        """Bytes read past the last newline; it dies with the connection."""
        self._pid = os.getpid()
        self._prefix = self._mint_prefix()
        """Unique per transport *instance*, so no two clients mint the same id."""
        self._ids = itertools.count(1)
        self._backoff = 0.0
        self._retry_at = 0.0
        with _REGISTRY_LOCK:
            _LIVE.add(self)

    @staticmethod
    def _mint_prefix() -> str:
        return f"herdr-pwa:{os.getpid()}:{token_hex(4)}"

    def _adopt_fork(self) -> None:
        """Become this process's own transport: new lock, new ids, no socket."""
        self._lock = threading.Lock()
        self._pid = os.getpid()
        self._prefix = self._mint_prefix()
        self._ids = itertools.count(1)
        self._backoff = 0.0
        self._retry_at = 0.0
        self._drop()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        state = "open" if self._sock is not None else "closed"
        return f"{type(self).__name__}(path={str(self.path)!r}, {state})"

    # -- public ------------------------------------------------------------

    def call(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float | None = None,
        idempotent: bool = True,
    ) -> dict[str, Any]:
        """Issue one request, return its reply object, raise on anything else."""
        budget = self.timeout if timeout is None else timeout
        if self._pid != os.getpid():  # forked without the hook running
            self._adopt_fork()
        with self._lock:
            payload = self._call_locked(method, params, budget, idempotent)
        raise_for_error_frame(payload)
        return payload

    def close(self) -> None:
        """Drop the connection. Idempotent, and safe to call while healthy."""
        with self._lock:
            self._drop()

    # -- the exchange ------------------------------------------------------

    def _call_locked(
        self, method: str, params: dict[str, Any], budget: float, idempotent: bool
    ) -> dict[str, Any]:
        """One exchange, with at most one retry on a connection that was reused."""
        call = _Call(method, self._clock() + budget, budget)
        try:
            return self._exchange(params, call)
        finally:
            # One request per connection, because that is what herdr speaks.
            self._drop()

    def _exchange(self, params: dict[str, Any], call: _Call) -> dict[str, Any]:
        request_id = f"{self._prefix}:{next(self._ids)}"
        frame = json.dumps(
            {"id": request_id, "method": call.method, "params": params},
            separators=(",", ":"),
        ).encode()
        # Before the connect, so an over-large call costs no socket and no wait.
        # @doc: docs/modules/engine/transport/MAX_REQUEST_BYTES.md
        if len(frame) > MAX_REQUEST_BYTES:
            raise RequestTooLarge(
                f"{call.method} would be {len(frame)} bytes on the wire and the "
                f"runner reads at most {MAX_REQUEST_BYTES}; nothing was sent",
                size=len(frame),
                limit=MAX_REQUEST_BYTES,
            )
        sock = self._connect(call)
        self._arm(sock, call)
        try:
            sock.sendall(frame + b"\n")
        except TimeoutError as exc:
            raise EngineTimeout(
                f"{call.method} could not be sent within {call.budget}s"
            ) from exc
        except OSError as exc:
            raise EngineUnavailable(f"{call.method} could not be sent: {exc}") from exc
        return self._receive(sock, request_id, call)

    def _receive(
        self, sock: socket.socket, request_id: str, call: _Call
    ) -> dict[str, Any]:
        """Read frames until the one addressed to ``request_id`` arrives."""
        for _ in range(MAX_STRAY_FRAMES):
            payload = self._decode(self._read_frame(sock, call))
            if payload.get("id") == request_id:
                return payload
            log.warning(
                "discarding a reply frame addressed to %r while waiting for %r",
                payload.get("id"),
                request_id,
            )
        raise InvalidResponse(
            f"no reply to {call.method} after {MAX_STRAY_FRAMES} frames for other "
            "ids; "
            "the connection is out of step and is being dropped"
        )

    def _read_frame(self, sock: socket.socket, call: _Call) -> bytes:
        """One newline-terminated frame, or a typed error. Never unbounded."""
        while True:
            line, sep, rest = self._buffer.partition(b"\n")
            if sep:
                self._buffer = rest
                return line
            if len(self._buffer) > self.max_frame_bytes:
                raise InvalidResponse(
                    f"reply to {call.method} exceeded {self.max_frame_bytes} bytes "
                    "with no frame terminator"
                )
            self._arm(sock, call)
            try:
                chunk = sock.recv(_CHUNK)
            except TimeoutError as exc:
                raise EngineTimeout(
                    f"{call.method} did not answer within {call.budget}s"
                ) from exc
            except OSError as exc:
                raise EngineUnavailable(
                    f"reading the reply to {call.method} failed: {exc}"
                ) from exc
            if not chunk:
                raise EngineUnavailable(
                    f"the runner closed the connection while answering {call.method}"
                )
            self._buffer += chunk

    _decode = staticmethod(_decode)

    # -- the connection ----------------------------------------------------

    def _connect(self, call: _Call) -> socket.socket:
        """The live connection, opening one if there is none."""
        if self._sock is not None:
            return self._sock
        now = self._clock()
        if now < self._retry_at:
            raise EngineUnavailable(
                f"no connection to the runner at {self.path}; not retrying for "
                f"another {self._retry_at - now:.1f}s",
                code="engine_unavailable",
            )
        self._check_budget(call, now)
        sock = self._new_socket(call)
        try:
            self._arm(sock, call)
            sock.connect(str(self.path))
        except BaseException as exc:
            sock.close()
            if isinstance(exc, TimeoutError):
                self._penalise()
                raise EngineTimeout(
                    f"connecting to the runner at {self.path} timed out after "
                    f"{call.budget}s"
                ) from exc
            if isinstance(exc, OSError):
                self._penalise()
                raise EngineUnavailable(self._why_unreachable(exc)) from exc
            raise
        self._backoff = 0.0
        self._retry_at = 0.0
        self._sock = sock
        self._buffer = b""
        return sock

    def _new_socket(self, call: _Call) -> socket.socket:
        """Create the descriptor, mapping the one failure that is not a connect."""
        try:
            return socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        except OSError as exc:
            self._penalise()
            why = f"could not open a socket to reach the runner at {self.path}: {exc}"
            if exc.errno in _EXHAUSTED:
                why += "; there are no file descriptors left to open one with"
            raise EngineUnavailable(why) from exc

    def _why_unreachable(self, exc: OSError) -> str:
        """Say which of the three "it isn't there" shapes this was."""
        return _why_unreachable(self.path, exc)

    def _penalise(self) -> None:
        self._backoff = min(max(self._backoff * 2, BACKOFF_BASE), self.backoff_cap)
        self._retry_at = self._clock() + self._backoff

    def _drop(self) -> None:
        """Destroy the connection and everything buffered on it."""
        sock, self._sock, self._buffer = self._sock, None, b""
        if sock is not None:
            try:
                sock.close()
            except OSError:  # pragma: no cover - close rarely fails
                log.debug("closing the runner socket failed", exc_info=True)

    def _arm(self, sock: socket.socket, call: _Call) -> None:
        """Re-arm the socket timeout from what is left of the call's budget."""
        sock.settimeout(self._check_budget(call, self._clock()))

    def _check_budget(self, call: _Call, now: float) -> float:
        """What is left of the call's budget, or :class:`EngineTimeout`."""
        remaining = call.remaining(now)
        if remaining <= 0:
            raise EngineTimeout(
                f"{call.method} ran out of its {call.budget}s budget talking to "
                f"the runner at {self.path}"
            )
        return remaining


# --------------------------------------------------------------------------
# The stream
# --------------------------------------------------------------------------


class EventStream:
    """One dedicated connection carrying a subscription and nothing else.

    @doc: docs/modules/engine/transport/EventStream.md
    """

    def __init__(
        self,
        path: Path | str,
        *,
        subscriptions: Sequence[Mapping[str, Any]],
        timeout: float,
        max_frame_bytes: int = MAX_FRAME_BYTES,
    ) -> None:
        self.path = Path(path)
        self.subscriptions = [dict(one) for one in subscriptions]
        """Fixed for this connection's whole life: the runner takes no second
        """
        self.timeout = timeout
        """Budget for the handshake only. Events are waited for indefinitely.
        """
        self.max_frame_bytes = max_frame_bytes
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._buffer = b""
        self._closed = False

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"{type(self).__name__}(path={str(self.path)!r}, "
            f"subscriptions={len(self.subscriptions)})"
        )

    # -- public ------------------------------------------------------------

    @property
    def closed(self) -> bool:
        """True once `close` was called, so a caller can tell a stop from a failure."""
        return self._closed

    def close(self) -> None:
        """End the iteration from any thread. Idempotent, and safe while healthy."""
        with self._lock:
            self._closed = True
            sock = self._sock
        if sock is None:
            return
        # A half-shutdown, not a close: the iterating thread still owns the
        # descriptor and must be the one to close it, or it reads a reused fd.
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:  # pragma: no cover - already gone
            log.debug("shutting down the event connection failed", exc_info=True)

    def open(self) -> socket.socket:
        """Subscribe now, so a live-but-quiet stream is tellable from a failed one.

        @doc: docs/modules/engine/transport/EventStream.open.md
        """
        sock = self._sock
        return sock if sock is not None else self._subscribe()

    def __iter__(self) -> Iterator[dict[str, Any]]:
        """Yield each event frame, subscribing first, until the runner or `close` ends it."""
        sock = self.open()
        try:
            while True:
                payload = _decode(self._read_frame(sock))
                raise_for_error_frame(payload)
                if payload.get("event"):
                    yield payload
                    continue
                log.warning(
                    "ignoring a frame on the event connection with no event: %r",
                    payload.get("id") or payload.get("result"),
                )
        finally:
            self._drop()

    # -- the handshake -----------------------------------------------------

    def _subscribe(self) -> socket.socket:
        """Open a connection and put its one and only request on it."""
        if not self.subscriptions:
            raise InvalidRequest(
                "an event stream with no subscriptions would hold a connection "
                "open forever and never deliver anything"
            )
        frame = json.dumps(
            {
                "id": f"herdr-pwa:{os.getpid()}:{token_hex(4)}:sub",
                "method": EVENT_SUBSCRIBE,
                "params": {"subscriptions": self.subscriptions},
            },
            separators=(",", ":"),
        ).encode()
        if len(frame) > MAX_REQUEST_BYTES:
            raise RequestTooLarge(
                f"subscribing to {len(self.subscriptions)} subscriptions would be "
                f"{len(frame)} bytes and the runner reads at most "
                f"{MAX_REQUEST_BYTES}; nothing was sent",
                size=len(frame),
                limit=MAX_REQUEST_BYTES,
            )
        sock = self._connect()
        # Every way this can fail has to give the descriptor back: a refused
        # subscription is a frame, not a hang-up, so nothing else closes it.
        try:
            sock.settimeout(self.timeout)
            try:
                sock.sendall(frame + b"\n")
            except OSError as exc:
                raise EngineUnavailable(
                    f"the subscribe could not be sent: {exc}"
                ) from exc
            self._await_started(sock)
        except BaseException:
            self._drop()
            raise
        # Events are minutes apart on a quiet herd, so there is no read budget
        # to arm; `close` is what ends the wait.
        sock.settimeout(None)
        return sock

    def _await_started(self, sock: socket.socket) -> None:
        """Read up to the confirmation, so a refusal is raised here, not later."""
        for _ in range(MAX_STRAY_FRAMES):
            payload = _decode(self._read_frame(sock))
            raise_for_error_frame(payload)
            result = payload.get("result")
            if isinstance(result, dict) and result.get("type") == SUBSCRIPTION_STARTED:
                return
            log.warning("discarding %r while waiting for the subscription", payload)
        raise InvalidResponse(
            f"the runner never confirmed the subscription after "
            f"{MAX_STRAY_FRAMES} frames"
        )

    def _connect(self) -> socket.socket:
        """A connection of this stream's own, registered so `close` can reach it."""
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        except OSError as exc:
            why = f"could not open a socket for the event stream: {exc}"
            if exc.errno in _EXHAUSTED:
                why += "; there are no file descriptors left to open one with"
            raise EngineUnavailable(why) from exc
        with self._lock:
            if self._closed:
                sock.close()
                raise EngineUnavailable("this event stream was closed before it opened")
            self._sock = sock
        try:
            sock.settimeout(self.timeout)
            sock.connect(str(self.path))
        except TimeoutError as exc:
            self._drop()
            raise EngineTimeout(
                f"connecting the event stream to {self.path} timed out after "
                f"{self.timeout}s"
            ) from exc
        except OSError as exc:
            self._drop()
            raise EngineUnavailable(_why_unreachable(self.path, exc)) from exc
        return sock

    # -- reading -----------------------------------------------------------

    def _read_frame(self, sock: socket.socket) -> bytes:
        """One newline-terminated frame, or a typed error. Never unbounded."""
        while True:
            line, sep, rest = self._buffer.partition(b"\n")
            if sep:
                self._buffer = rest
                return line
            if len(self._buffer) > self.max_frame_bytes:
                raise InvalidResponse(
                    f"an event frame exceeded {self.max_frame_bytes} bytes with "
                    "no terminator"
                )
            try:
                chunk = sock.recv(_CHUNK)
            except TimeoutError as exc:
                raise EngineTimeout(
                    f"the runner did not confirm the subscription within "
                    f"{self.timeout}s"
                ) from exc
            except OSError as exc:
                raise EngineUnavailable(f"the event stream failed: {exc}") from exc
            if not chunk:
                raise EngineUnavailable(
                    "the runner closed the event connection"
                    if not self._closed
                    else "the event stream was closed"
                )
            self._buffer += chunk

    def _drop(self) -> None:
        """Destroy the connection and everything buffered on it."""
        with self._lock:
            sock, self._sock, self._buffer = self._sock, None, b""
        if sock is not None:
            try:
                sock.close()
            except OSError:  # pragma: no cover - close rarely fails
                log.debug("closing the event connection failed", exc_info=True)
