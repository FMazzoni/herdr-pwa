"""The wire: AF_UNIX connections to the runner, and the error taxonomy."""

import errno
import itertools
import json
import logging
import os
import socket
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from secrets import token_hex
from typing import Final, NamedTuple, Protocol, runtime_checkable

from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError

from herdr_pwa.clock import Clock
from herdr_pwa.engine.generated import ErrorBody, SuccessResponse
from herdr_pwa.engine.generated.success_response import (
    ResponseResult,
    SubscriptionStartedResponseResult,
)

# One herdr socket frame, decoded: the object as the runner sent it.
type Frame = dict[str, JsonValue]


class _Refusal(BaseModel):
    """A frame read as a refusal: its ``error`` object, whatever else it carries."""

    error: ErrorBody


class WireReply(NamedTuple):
    """One decoded frame, read once against herdr's published schema."""

    payload: Frame
    # The success arm the schema reads, or why it reads none: an event, a refusal, a drift.
    result: ResponseResult | ValidationError
    # The refusal an ``error`` object reports, if the frame carries one.
    error: ErrorBody | None


log = logging.getLogger(__name__)

MAX_FRAME_BYTES: Final = 4 * 1024 * 1024

MAX_REQUEST_BYTES: Final = 1024 * 1024

MAX_STRAY_FRAMES: Final = 8

BACKOFF_BASE: Final = 0.1

BACKOFF_CAP: Final = 2.0

_CHUNK: Final = 65536

EVENT_SUBSCRIBE: Final = "events.subscribe"

_EXHAUSTED: Final = frozenset({errno.EMFILE, errno.ENFILE})


class EngineError(Exception):
    """Any failure talking to the agent runner."""

    code = "engine_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        # True when a write's whole frame went out and its answer was lost: it may have run.
        self.after_send = False
        if code:
            self.code = code


class EngineMisconfigured(EngineError):  # noqa: N818 - the names the rest of the app maps to HTTP statuses
    """This box's configuration will not load, so no client can be built (-> 500)."""

    code = "engine_misconfigured"


class EngineUnavailable(EngineError):  # noqa: N818 - the names the rest of the app maps to HTTP statuses
    """The runner is not running, or its socket is stale or unreachable (-> 503)."""

    code = "engine_unavailable"


class EngineTimeout(EngineError):  # noqa: N818 - the names the rest of the app maps to HTTP statuses
    """The runner did not answer inside the timeout (-> 503)."""

    code = "engine_timeout"


class InvalidRequest(EngineError, ValueError):  # noqa: N818 - the names the rest of the app maps to HTTP statuses
    """The caller asked for something this client cannot send (-> 400)."""

    code = "invalid_request"


class RequestTooLarge(InvalidRequest):
    """Past what the runner will read, so it is refused here and never sent (-> 400)."""

    code = "request_too_large"

    def __init__(self, message: str, *, size: int, limit: int) -> None:
        super().__init__(message)
        self.size = size
        self.limit = limit


class InvalidResponse(EngineError):  # noqa: N818 - the names the rest of the app maps to HTTP statuses
    """The runner answered something that is not the JSON contract (-> 502)."""

    code = "invalid_response"


class TargetNotFound(EngineError):  # noqa: N818 - the names the rest of the app maps to HTTP statuses
    """The addressed agent vanished mid-flight (-> 404)."""

    code = "target_not_found"


class ProtocolMismatch(EngineError):  # noqa: N818 - the names the rest of the app maps to HTTP statuses
    """The runner speaks an unexpected snapshot protocol, so writes fail closed (-> 409)."""

    code = "protocol_mismatch"


_NOT_FOUND_CODES: Final = frozenset(
    {"agent_not_found", "pane_not_found", "target_not_found", "tab_not_found"}
)

_UNAVAILABLE_CODES: Final = frozenset(
    {"server_not_running", "socket_not_found", "connection_refused"}
)


def raise_for_error_frame(reply: WireReply) -> None:
    """Map a peer-reported error frame onto the taxonomy, or return."""
    body = reply.error
    if body is None:
        return
    code = body.code or "engine_error"
    message = body.message or code
    if code in _NOT_FOUND_CODES:
        raise TargetNotFound(message, code=code)
    if code in _UNAVAILABLE_CODES:
        raise EngineUnavailable(message, code=code)
    raise EngineError(message, code=code)


def _open_socket(purpose: str) -> socket.socket:
    """A fresh Unix stream socket, its one failure that is not a connect said in words."""
    try:
        return socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except OSError as exc:
        why = f"could not open a socket {purpose}: {exc}"
        if exc.errno in _EXHAUSTED:
            why += "; there are no file descriptors left to open one with"
        raise EngineUnavailable(why) from exc


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


_FRAME: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


def decode_frame(line: bytes) -> WireReply:
    """Bytes to a typed reply, with both ways of not being an object mapped."""
    try:
        payload = _FRAME.validate_json(line.decode("utf-8", errors="replace"))
    except ValidationError as exc:
        raise InvalidResponse(f"the runner sent a frame that is not JSON: {line[:200]!r}") from exc
    if not isinstance(payload, dict):
        raise InvalidResponse(f"expected a JSON object, got {type(payload).__name__}")
    result: ResponseResult | ValidationError
    try:
        result = SuccessResponse.model_validate(payload).result
    except ValidationError as exc:
        result = exc
    error = payload.get("error")
    body: ErrorBody | None = None
    if isinstance(error, dict):
        try:
            body = _Refusal.model_validate(payload).error
        except ValidationError:
            # Still a refusal: an error the schema does not describe is reported, not obeyed.
            body = ErrorBody(code="engine_error", message=f"the runner reported {error!r:.200}")
    return WireReply(payload, result, body)


def _next_frame(
    buffer: bytearray, recv: Callable[[], bytes], *, limit: int, too_long: str
) -> bytes:
    """Cut one newline-terminated frame off *buffer*, reading more as needed, never past *limit*."""
    while True:
        end = buffer.find(b"\n")
        if end >= 0:
            line = bytes(buffer[:end])
            del buffer[: end + 1]
            return line
        if len(buffer) > limit:
            raise InvalidResponse(too_long)
        buffer += recv()


def _confirms_subscription(reply: WireReply) -> bool:
    result = reply.result
    return isinstance(result, ResponseResult) and isinstance(
        result.root, SubscriptionStartedResponseResult
    )


# --------------------------------------------------------------------------
# The seam
# --------------------------------------------------------------------------


class _Call(NamedTuple):
    """One call's identity and its *one* time budget."""

    method: str
    deadline: float
    budget: float
    # A read whose answer is lost did nothing; a write's may have run.
    idempotent: bool = True

    def remaining(self, now: float) -> float:
        return self.deadline - now


@runtime_checkable
class Transport(Protocol):
    """What :class:`~herdr_pwa.engine.client.HerdrClient` needs from a wire."""

    def call(
        self,
        method: str,
        params: Frame,
        *,
        timeout: float | None = None,
        idempotent: bool = True,
    ) -> WireReply:
        """Issue one request and return its decoded reply."""
        ...

    def close(self) -> None:
        """Release whatever is held open. Calling it twice must be harmless."""
        ...


class SocketTransport:
    """Newline-delimited JSON-RPC over AF_UNIX, one fresh connection per call.

    Calls run in parallel: the lock guards only the id counter and the backoff fields.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        timeout: float,
        clock: Clock = time.monotonic,
    ) -> None:
        self.path = Path(path)
        self.timeout = timeout
        self.max_frame_bytes = MAX_FRAME_BYTES
        self._clock = clock
        self._lock = threading.Lock()
        self._pid = os.getpid()
        self._prefix = self._mint_prefix()
        self._ids = itertools.count(1)
        self._backoff = 0.0
        self._retry_at = 0.0

    @staticmethod
    def _mint_prefix() -> str:
        return f"herdr-pwa:{os.getpid()}:{token_hex(4)}"

    def adopt_fork(self) -> None:
        """Become this process's own transport: a lock no parent thread holds, and new ids."""
        self._lock = threading.Lock()
        self._pid = os.getpid()
        self._prefix = self._mint_prefix()
        self._ids = itertools.count(1)
        self._backoff = 0.0
        self._retry_at = 0.0

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{type(self).__name__}(path={str(self.path)!r})"

    # -- public ------------------------------------------------------------

    def call(
        self,
        method: str,
        params: Frame,
        *,
        timeout: float | None = None,
        idempotent: bool = True,
    ) -> WireReply:
        """Issue one request, return its decoded reply, raise on anything else."""
        budget = self.timeout if timeout is None else timeout
        # The budget starts when the caller asks, not when some shared resource frees up.
        call = _Call(method, self._clock() + budget, budget, idempotent)
        # A forked child may inherit the lock mid-hold; nothing here forks, so a pid check is enough.
        if self._pid != os.getpid():
            self.adopt_fork()
        with self._lock:
            request_id = f"{self._prefix}:{next(self._ids)}"
        reply = self._exchange(params, call, request_id)
        raise_for_error_frame(reply)
        return reply

    def close(self) -> None:
        """Release nothing: every call opens and closes its own connection. Idempotent."""

    # -- the exchange ------------------------------------------------------

    def _exchange(self, params: Frame, call: _Call, request_id: str) -> WireReply:
        """One exchange; never retried, since one request per connection is what herdr speaks."""
        frame = json.dumps(
            {"id": request_id, "method": call.method, "params": params},
            separators=(",", ":"),
        ).encode()
        # Before the connect, so an over-large call costs no socket and no wait.
        if len(frame) > MAX_REQUEST_BYTES:
            raise RequestTooLarge(
                f"{call.method} would be {len(frame)} bytes on the wire and the "
                f"runner reads at most {MAX_REQUEST_BYTES}; nothing was sent",
                size=len(frame),
                limit=MAX_REQUEST_BYTES,
            )
        # One request per connection, because that is what herdr speaks.
        with self._connect(call) as sock:
            self._arm(sock, call)
            try:
                sock.sendall(frame + b"\n")
            except TimeoutError as exc:
                raise EngineTimeout(
                    f"{call.method} could not be sent within {call.budget}s"
                ) from exc
            except OSError as exc:
                raise EngineUnavailable(f"{call.method} could not be sent: {exc}") from exc
            # The whole frame is out, newline and all; from here a failure cannot say nothing ran.
            try:
                return self._receive(sock, request_id, call)
            except EngineError as exc:
                exc.after_send = not call.idempotent
                raise

    def _receive(self, sock: socket.socket, request_id: str, call: _Call) -> WireReply:
        """Read frames until the one addressed to ``request_id`` arrives."""
        buffer = bytearray()
        too_long = (
            f"reply to {call.method} exceeded {self.max_frame_bytes} bytes with no frame terminator"
        )
        for _ in range(MAX_STRAY_FRAMES):
            line = _next_frame(
                buffer,
                lambda: self._recv(sock, call),
                limit=self.max_frame_bytes,
                too_long=too_long,
            )
            reply = self._decode(line)
            if reply.payload.get("id") == request_id:
                return reply
            log.warning(
                "discarding a reply frame addressed to %r while waiting for %r",
                reply.payload.get("id"),
                request_id,
            )
        raise InvalidResponse(
            f"no reply to {call.method} after {MAX_STRAY_FRAMES} frames for other "
            "ids; "
            "the connection is out of step and is being dropped"
        )

    def _recv(self, sock: socket.socket, call: _Call) -> bytes:
        """One non-empty chunk of the reply, with every failure typed."""
        self._arm(sock, call)
        try:
            chunk = sock.recv(_CHUNK)
        except TimeoutError as exc:
            raise EngineTimeout(f"{call.method} did not answer within {call.budget}s") from exc
        except OSError as exc:
            raise EngineUnavailable(f"reading the reply to {call.method} failed: {exc}") from exc
        if not chunk:
            raise EngineUnavailable(
                f"the runner closed the connection while answering {call.method}"
            )
        return chunk

    _decode = staticmethod(decode_frame)

    # -- the connection ----------------------------------------------------

    def _connect(self, call: _Call) -> socket.socket:
        """A new connection of this call's own, unless a recent failure says not to dial yet."""
        with self._lock:
            now, retry_at = self._clock(), self._retry_at
        if now < retry_at:
            raise EngineUnavailable(
                f"no connection to the runner at {self.path}; not retrying for "
                f"another {retry_at - now:.1f}s",
                code="engine_unavailable",
            )
        self._check_budget(call, now)
        sock = self._new_socket()
        try:
            self._arm(sock, call)
            sock.connect(str(self.path))
        except BaseException as exc:
            sock.close()
            if isinstance(exc, TimeoutError):
                self._penalise()
                raise EngineTimeout(
                    f"connecting to the runner at {self.path} timed out after {call.budget}s"
                ) from exc
            if isinstance(exc, OSError):
                self._penalise()
                raise EngineUnavailable(self._why_unreachable(exc)) from exc
            raise
        with self._lock:
            self._backoff = 0.0
            self._retry_at = 0.0
        return sock

    def _new_socket(self) -> socket.socket:
        """Create the descriptor; failing to is a failed attempt, and backs off like one."""
        try:
            return _open_socket(f"to reach the runner at {self.path}")
        except EngineUnavailable:
            self._penalise()
            raise

    def _why_unreachable(self, exc: OSError) -> str:
        """Say which of the three "it isn't there" shapes this was."""
        return _why_unreachable(self.path, exc)

    def _penalise(self) -> None:
        with self._lock:
            self._backoff = min(max(self._backoff * 2, BACKOFF_BASE), BACKOFF_CAP)
            self._retry_at = self._clock() + self._backoff

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
    """One dedicated connection carrying a subscription and nothing else."""

    def __init__(
        self,
        path: Path | str,
        *,
        subscriptions: Sequence[Mapping[str, JsonValue]],
        timeout: float,
        max_frame_bytes: int = MAX_FRAME_BYTES,
    ) -> None:
        self.path = Path(path)
        self.subscriptions = [dict(one) for one in subscriptions]
        self.timeout = timeout
        self.max_frame_bytes = max_frame_bytes
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._buffer = bytearray()
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
        # Shutdown, not close: the iterating thread owns the fd and would otherwise read a reused one.
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:  # pragma: no cover - already gone
            log.debug("shutting down the event connection failed", exc_info=True)

    def open(self) -> socket.socket:
        """Subscribe now, so a live-but-quiet stream is tellable from a failed one."""
        sock = self._sock
        return sock if sock is not None else self._subscribe()

    def __iter__(self) -> Iterator[Frame]:
        """Yield each event frame, subscribing first, until the runner or `close` ends it."""
        sock = self.open()
        try:
            while True:
                reply = decode_frame(self._read_frame(sock))
                raise_for_error_frame(reply)
                payload = reply.payload
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
        # A refused subscription is a frame, not a hang-up, so nothing else gives the fd back.
        try:
            sock.settimeout(self.timeout)
            try:
                sock.sendall(frame + b"\n")
            except OSError as exc:
                raise EngineUnavailable(f"the subscribe could not be sent: {exc}") from exc
            self._await_started(sock)
        except BaseException:
            self._drop()
            raise
        # Events are minutes apart on a quiet herd; `close` is what ends the wait.
        sock.settimeout(None)
        return sock

    def _await_started(self, sock: socket.socket) -> None:
        """Read up to the confirmation, so a refusal is raised here, not later."""
        for _ in range(MAX_STRAY_FRAMES):
            reply = decode_frame(self._read_frame(sock))
            raise_for_error_frame(reply)
            if _confirms_subscription(reply):
                return
            log.warning("discarding %r while waiting for the subscription", reply.payload)
        raise InvalidResponse(
            f"the runner never confirmed the subscription after {MAX_STRAY_FRAMES} frames"
        )

    def _connect(self) -> socket.socket:
        """A connection of this stream's own, registered so `close` can reach it."""
        sock = _open_socket("for the event stream")
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
                f"connecting the event stream to {self.path} timed out after {self.timeout}s"
            ) from exc
        except OSError as exc:
            self._drop()
            raise EngineUnavailable(_why_unreachable(self.path, exc)) from exc
        return sock

    # -- reading -----------------------------------------------------------

    def _read_frame(self, sock: socket.socket) -> bytes:
        return _next_frame(
            self._buffer,
            lambda: self._recv(sock),
            limit=self.max_frame_bytes,
            too_long=f"an event frame exceeded {self.max_frame_bytes} bytes with no terminator",
        )

    def _recv(self, sock: socket.socket) -> bytes:
        """One non-empty chunk of the stream, with every failure typed."""
        try:
            chunk = sock.recv(_CHUNK)
        except TimeoutError as exc:
            raise EngineTimeout(
                f"the runner did not confirm the subscription within {self.timeout}s"
            ) from exc
        except OSError as exc:
            raise EngineUnavailable(f"the event stream failed: {exc}") from exc
        if not chunk:
            raise EngineUnavailable(
                "the runner closed the event connection"
                if not self._closed
                else "the event stream was closed"
            )
        return chunk

    def _drop(self) -> None:
        """Destroy the connection and everything buffered on it."""
        with self._lock:
            sock, self._sock, self._buffer = self._sock, None, bytearray()
        if sock is not None:
            try:
                sock.close()
            except OSError:  # pragma: no cover - close rarely fails
                log.debug("closing the event connection failed", exc_info=True)
