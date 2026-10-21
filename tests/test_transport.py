"""Transport tests, against a real AF_UNIX server rather than a mock."""

import contextlib
import inspect
import itertools
import json
import logging
import os
import socket
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import JsonValue, TypeAdapter

from herdr_pwa.clock import MonoClock, MonoTime, mono_now
from herdr_pwa.engine import (
    EngineError,
    EngineTimeoutError,
    EngineUnavailableError,
    EventStream,
    InvalidRequestError,
    InvalidResponseError,
    RequestTooLargeError,
    TargetNotFoundError,
    Transport,
    WireLimits,
)
from tests._jsonpath import at
from tests.engine_seam import (
    EVENT_SUBSCRIBE,
    MAX_REQUEST_BYTES,
    WIRE_LIMITS,
    SocketTransport,
)
from tests.waiting import eventually, until

SRC = Path(__file__).resolve().parent.parent / "src" / "herdr_pwa"

# Short enough that a timeout test is quick, long enough for a loaded CI box.
FAST = 0.4
# A dribbling peer's pause between bytes: each recv returns inside any per-recv timeout.
DRIBBLE_GAP = 0.02
# How long a hang-up may take to reach a socket that keeps sending into it.
HANG_UP_SECONDS = 2.0
REQUEST: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])

Handler = Callable[[dict[str, JsonValue], socket.socket], dict[str, JsonValue] | bytes | None]


def open_fds() -> int:
    """Descriptors this process holds open. Linux-only, like the runner."""
    return len(list(Path(f"/proc/{os.getpid()}/fd").iterdir()))


def ok(request: dict[str, JsonValue], _conn: socket.socket) -> dict[str, JsonValue]:
    """The minimal well-formed reply: right id, an ``ok`` result arm."""
    return {"id": request["id"], "result": {"type": "ok", "echo": request["method"]}}


class FakeHerdr:
    """A throwaway runner: binds, accepts, answers once, and hangs up."""

    def __init__(self, path: Path, handler: Handler = ok) -> None:
        self.path = path
        self.handler = handler
        self.requests: list[dict[str, JsonValue]] = []
        self.connections = 0
        # Requests per accepted connection; anything but 1 is a request sent after the hang-up.
        self.per_connection: list[int] = []
        self._stop = threading.Event()
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(path))
        self._server.listen(16)
        self._server.settimeout(0.05)
        self._accepting = threading.Thread(target=self._serve, daemon=True)
        self._accepting.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn = self._server.accept()[0]
            except (TimeoutError, OSError):
                continue
            self.connections += 1
            self.per_connection.append(0)
            index = len(self.per_connection) - 1
            threading.Thread(target=self._session, args=(conn, index), daemon=True).start()

    def _session(self, conn: socket.socket, index: int) -> None:
        buffer = b""
        with conn:
            conn.settimeout(0.05)
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(65536)
                except TimeoutError:
                    continue
                except OSError:
                    return
                if not chunk:
                    return
                buffer += chunk
                while b"\n" in buffer:
                    line, _, buffer = buffer.partition(b"\n")
                    # A frame that is not an object fails here, loudly, as the client's bug.
                    request = REQUEST.validate_json(line)
                    self.requests.append(request)
                    self.per_connection[index] += 1
                    if not self._answer(conn, request):
                        return

    def _answer(self, conn: socket.socket, request: dict[str, JsonValue]) -> bool:
        """Answer once, then hang up. Returns False to end the session."""
        reply = self.handler(request, conn)
        if reply is None:
            return True
        payload = reply if isinstance(reply, bytes) else _line(reply)
        try:
            conn.sendall(payload)
        except OSError:
            return False
        return False  # one request per connection, because that is herdr

    def close(self) -> None:
        self._stop.set()
        self._server.close()
        self._accepting.join(timeout=2)

    @property
    def methods(self) -> list[JsonValue]:
        return [r["method"] for r in self.requests]


def _line(payload: dict[str, JsonValue]) -> bytes:
    return json.dumps(payload).encode() + b"\n"


@pytest.fixture
def sock_path(tmp_path: Path) -> Path:
    return tmp_path / "herdr.sock"


@pytest.fixture
def server(sock_path: Path) -> Iterator[FakeHerdr]:
    """A healthy runner. Tests that want a hostile one build their own."""
    fake = FakeHerdr(sock_path)
    yield fake
    fake.close()


def serve(path: Path, handler: Handler, request: pytest.FixtureRequest) -> FakeHerdr:
    fake = FakeHerdr(path, handler)
    request.addfinalizer(fake.close)
    return fake


class Tracked(SocketTransport):
    """A transport that keeps every socket it made, so a test can see each one was closed."""

    def __init__(
        self, path: Path, *, timeout: float = FAST, limits: WireLimits = WIRE_LIMITS
    ) -> None:
        super().__init__(path, timeout=timeout, limits=limits)
        self.opened: list[socket.socket] = []

    def _new_socket(self) -> socket.socket:
        self.opened.append(super()._new_socket())
        return self.opened[-1]

    @property
    def still_open(self) -> list[socket.socket]:
        return [s for s in self.opened if s.fileno() != -1]


def transport(
    path: Path,
    *,
    timeout: float = FAST,
    clock: MonoClock = mono_now,
    limits: WireLimits = WIRE_LIMITS,
) -> SocketTransport:
    return SocketTransport(path, timeout=timeout, clock=clock, limits=limits)


# --------------------------------------------------------------------------
# The happy path, and the framing itself
# --------------------------------------------------------------------------


def test_the_request_is_one_newline_terminated_json_object(
    server: FakeHerdr, sock_path: Path
) -> None:
    """The whole protocol, asserted on the bytes rather than on an intention."""
    wire = transport(sock_path)
    reply = wire.call("session.snapshot", {})
    assert at(reply.payload, "result", "echo") == "session.snapshot"
    assert server.requests[0]["method"] == "session.snapshot"
    assert server.requests[0]["params"] == {}
    assert set(server.requests[0]) == {"id", "method", "params"}


def test_params_are_passed_through_unmangled(server: FakeHerdr, sock_path: Path) -> None:
    """Named params, so no argv escaping question: dashes, newlines, unicode."""
    wire = transport(sock_path)
    params: dict[str, JsonValue] = {"target": "w1:p3", "text": "--not-a-flag\nsecond line ✳"}
    wire.call("agent.prompt", params, idempotent=False)
    assert server.requests[0]["params"] == params


def test_ids_are_unique(server: FakeHerdr, sock_path: Path) -> None:
    wire = transport(sock_path)
    for _ in range(5):
        wire.call("agent.list", {})
    ids = [r["id"] for r in server.requests]
    assert len(set(ids)) == 5
    assert all(str(os.getpid()) in str(i) for i in ids)


def test_a_reply_is_matched_to_the_id_that_asked_for_it(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """Deleting the id check must break this, and it did not used to."""
    first: list[JsonValue] = []

    def handler(req: dict[str, JsonValue], _conn: socket.socket) -> dict[str, JsonValue]:
        if not first:
            first.append(req["id"])
        return {"id": first[0], "result": {"type": "ok", "echo": req["method"]}}

    serve(sock_path, handler, request)
    wire = transport(sock_path, timeout=0.3)
    assert at(wire.call("session.snapshot", {}).payload, "result", "echo") == "session.snapshot"
    with pytest.raises(EngineError):  # never a reply, and never the wrong one
        wire.call("agent.list", {})  # answered under the first call's id


def test_two_transports_never_mint_the_same_id(server: FakeHerdr, sock_path: Path) -> None:
    """Ids are per instance, so two clients on one runner cannot collide."""
    one, two = transport(sock_path), transport(sock_path)
    one.call("agent.list", {})
    two.call("agent.list", {})
    assert server.requests[0]["id"] != server.requests[1]["id"]


def test_each_call_gets_its_own_connection(server: FakeHerdr, sock_path: Path) -> None:
    """One request per connection, because that is what herdr speaks."""
    wire = transport(sock_path)
    for _ in range(10):
        wire.call("agent.list", {})
    assert server.connections == 10


class NameOnly:
    """Everything the Protocol's ``isinstance`` looks at, and nothing it means."""

    def call(self) -> None:  # wrong signature, no reply, no wire
        pass


@pytest.mark.usefixtures("server")
def test_satisfies_the_transport_protocol(sock_path: Path) -> None:
    """``isinstance`` on a ``@runtime_checkable`` Protocol is not a conformance test."""
    assert isinstance(NameOnly(), Transport), "isinstance only checks names"
    pairs = (("call", SocketTransport.call, Transport.call),)
    for name, real, declared in pairs:
        assert inspect.signature(real) == inspect.signature(declared), (
            f"{name} does not have the signature the Protocol declares"
        )

    wire: Transport = transport(sock_path)
    reply = wire.call("session.snapshot", {}, timeout=FAST, idempotent=True)
    assert at(reply.payload, "result", "echo") == "session.snapshot"


# --------------------------------------------------------------------------
# Errors the runner reports. These are not transport failures.
# --------------------------------------------------------------------------


def test_an_error_frame_maps_onto_the_taxonomy(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """Pinned to the envelope herdr 0.8.0 actually sends."""

    def handler(req: dict[str, JsonValue], _c: socket.socket) -> dict[str, JsonValue]:
        return {
            "id": req["id"],
            "error": {
                "code": "agent_not_found",
                "message": "agent target nosuch not found",
            },
        }

    serve(sock_path, handler, request)
    wire = transport(sock_path)
    with pytest.raises(TargetNotFoundError) as exc:
        wire.call("agent.get", {"target": "nosuch"})
    assert exc.value.code == "agent_not_found"
    assert "nosuch" in exc.value.message


def test_a_server_side_unavailable_code_is_not_a_transport_failure(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    def handler(req: dict[str, JsonValue], _c: socket.socket) -> dict[str, JsonValue]:
        return {
            "id": req["id"],
            "error": {"code": "server_not_running", "message": "no server"},
        }

    serve(sock_path, handler, request)
    wire = transport(sock_path)
    with pytest.raises(EngineUnavailableError):
        wire.call("session.snapshot", {})


def test_an_unknown_error_code_stays_a_plain_engine_error(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    def handler(req: dict[str, JsonValue], _c: socket.socket) -> dict[str, JsonValue]:
        return {"id": req["id"], "error": {"code": "weird_new_code", "message": "hm"}}

    serve(sock_path, handler, request)
    wire = transport(sock_path)
    with pytest.raises(EngineError) as exc:
        wire.call("agent.get", {"target": "w1:p3"})
    assert type(exc.value) is EngineError
    assert exc.value.code == "weird_new_code"


def test_an_error_body_the_schema_does_not_describe_is_still_a_refusal(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """Reported as a plain engine error, never obeyed and never mistaken for a reply."""

    def handler(req: dict[str, JsonValue], _c: socket.socket) -> dict[str, JsonValue]:
        return {"id": req["id"], "error": {"code": 7}}

    serve(sock_path, handler, request)
    wire = transport(sock_path)
    with pytest.raises(EngineError) as exc:
        wire.call("agent.get", {"target": "w1:p3"})
    assert type(exc.value) is EngineError
    assert exc.value.code == "engine_error"
    assert exc.value.message == "the runner reported {'code': 7}"


def test_an_error_that_is_not_an_object_is_left_to_the_reply_reader(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """Only an object under ``error`` is a refusal; the client then refuses the reply's shape."""

    def handler(req: dict[str, JsonValue], _c: socket.socket) -> dict[str, JsonValue]:
        return {"id": req["id"], "error": "boom"}

    serve(sock_path, handler, request)
    wire = transport(sock_path)
    reply = wire.call("agent.get", {"target": "w1:p3"})
    assert at(reply.payload, "error") == "boom"


def test_an_error_frame_is_a_conversation_not_a_failure(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """A refused request must not poison the next one."""
    state = {"n": 0}

    def handler(req: dict[str, JsonValue], _c: socket.socket) -> dict[str, JsonValue]:
        state["n"] += 1
        if state["n"] == 1:
            return {
                "id": req["id"],
                "error": {"code": "pane_not_found", "message": "gone"},
            }
        return ok(req, _c)

    fake = serve(sock_path, handler, request)
    wire = transport(sock_path)
    with pytest.raises(TargetNotFoundError):
        wire.call("agent.get", {"target": "gone"})
    # Each call gets its own connection, which is the protocol rather than a symptom.
    wire.call("agent.get", {"target": "here"})
    assert fake.connections == 2


# --------------------------------------------------------------------------
# "herdr isn't there" — three shapes, one error class
# --------------------------------------------------------------------------


def test_a_missing_socket_is_unavailable_not_a_filenotfounderror(
    tmp_path: Path,
) -> None:
    wire = transport(tmp_path / "nothing-here.sock")
    with pytest.raises(EngineUnavailableError) as exc:
        wire.call("session.snapshot", {})
    assert "not running" in exc.value.message


def test_a_stale_socket_file_is_unavailable_not_a_connectionrefusederror(
    sock_path: Path,
) -> None:
    """The socket a killed runner left behind: bound, listening to nobody."""
    orphan = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    orphan.bind(str(sock_path))  # bound but never listen()ed
    try:
        wire = transport(sock_path)
        with pytest.raises(EngineUnavailableError) as exc:
            wire.call("session.snapshot", {})
        assert "stale" in exc.value.message
    finally:
        orphan.close()


def test_a_path_that_is_not_a_socket_is_unavailable_not_an_oserror(
    tmp_path: Path,
) -> None:
    plain = tmp_path / "not-a-socket"
    plain.write_text("i am a regular file")
    wire = transport(plain)
    with pytest.raises(EngineUnavailableError):
        wire.call("session.snapshot", {})


def test_the_runner_hanging_up_mid_answer_is_unavailable(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    def handler(_req: dict[str, JsonValue], conn: socket.socket) -> None:
        conn.close()

    serve(sock_path, handler, request)
    wire = transport(sock_path)
    with pytest.raises(EngineUnavailableError) as exc:
        wire.call("session.snapshot", {})
    assert "closed the connection" in exc.value.message


# --------------------------------------------------------------------------
# Timeouts bound the call, not the syscall
# --------------------------------------------------------------------------


def test_a_runner_that_never_answers_times_out(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    serve(sock_path, lambda _r, _c: None, request)
    wire = transport(sock_path, timeout=0.2)
    started = time.monotonic()
    with pytest.raises(EngineTimeoutError):
        wire.call("session.snapshot", {})
    assert time.monotonic() - started < 2.0


def test_a_dribbling_peer_cannot_outlast_the_deadline(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """The bound is on the whole read, not on each ``recv``."""

    def handler(_req: dict[str, JsonValue], conn: socket.socket) -> None:
        for _ in range(200):
            try:
                conn.sendall(b" ")
            except OSError:
                return
            # Pacing, not waiting: the peer's slowness between bytes is what is under test.
            time.sleep(DRIBBLE_GAP)

    serve(sock_path, handler, request)
    wire = transport(sock_path, timeout=0.3)
    started = time.monotonic()
    with pytest.raises(EngineTimeoutError):
        wire.call("session.snapshot", {})
    assert time.monotonic() - started < 2.0


def test_a_timeout_destroys_the_connection(sock_path: Path, request: pytest.FixtureRequest) -> None:
    """Otherwise the late reply is handed to whoever asks next."""
    # The reply is held until the call has given up, however long that takes under load.
    release = threading.Event()

    def handler(req: dict[str, JsonValue], conn: socket.socket) -> None:
        release.wait(10)
        with contextlib.suppress(OSError):
            conn.sendall(_line(ok(req, conn)))

    serve(sock_path, handler, request)
    wire = Tracked(sock_path, timeout=0.15)
    try:
        with pytest.raises(EngineTimeoutError):
            wire.call("agent.get", {"target": "w1:p3"})
    finally:
        release.set()
    assert len(wire.opened) == 1
    assert wire.still_open == []


def test_an_exhausted_budget_times_out_before_dialling(sock_path: Path) -> None:
    """A zero timeout must not become an unbounded connect -- and must not leak."""
    opened: list[socket.socket] = []

    # Only its own descriptors: other tests' threads close theirs in this process meanwhile.
    class Counted(SocketTransport):
        def _new_socket(self) -> socket.socket:
            opened.append(super()._new_socket())
            return opened[-1]

    wire = Counted(sock_path, timeout=0.0)
    retained: list[EngineTimeoutError] = []  # what logging.exception would hold
    for _ in range(51):
        with pytest.raises(EngineTimeoutError) as caught:
            wire.call("session.snapshot", {})
        retained.append(caught.value)
    assert [s for s in opened if s.fileno() != -1] == [], "a timed-out call kept its socket open"
    assert opened == [], f"{len(opened)} sockets made for calls with no budget to dial"
    assert len(retained) == 51  # the tracebacks are still alive at this point


def test_every_wait_in_a_call_is_armed_from_its_one_deadline(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """``timeout=T`` must mean T for the *call*, whatever it does inside.

    The clock moves a fixed step each time the transport reads it, so what each wait was armed
    with is decided by the code, not by how long the machine took.
    """
    answered: list[int] = []

    def handler(req: dict[str, JsonValue], conn: socket.socket) -> dict[str, JsonValue] | None:
        answered.append(1)
        return ok(req, conn) if len(answered) == 1 else None

    serve(sock_path, handler, request)
    budget = 0.2
    reads = itertools.count()
    armed: list[float] = []

    class Recording(socket.socket):
        def settimeout(self, value: float | None) -> None:
            if value is not None:
                armed.append(value)
            super().settimeout(value)

    class Watched(SocketTransport):
        def _new_socket(self) -> socket.socket:
            return Recording(socket.AF_UNIX, socket.SOCK_STREAM)

    wire = Watched(sock_path, timeout=budget, clock=lambda: MonoTime(next(reads) * budget / 20))
    wire.call("agent.list", {})  # answered, and hung up on, like every call
    armed.clear()
    with pytest.raises(EngineTimeoutError) as caught:  # the second one is never answered at all
        wire.call("agent.get", {"target": "w1:p3"})
    assert len(armed) >= 3, armed
    assert armed[0] < budget, f"one budget, not a fresh one per wait: {armed}"
    assert all(later < earlier for earlier, later in itertools.pairwise(armed)), armed
    assert str(budget) in str(caught.value), "the message must name the real budget"


@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded")
def test_a_forked_child_gets_its_own_ids_and_its_own_connection(  # noqa: PLR0915 - a fork scenario whose steps only make sense in order
    server: FakeHerdr, sock_path: Path
) -> None:
    """A preforking server must not hand two workers one stream and one id space."""
    wire = transport(sock_path)
    wire.call("agent.list", {})  # parent: prefix minted, socket open, counter used

    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:  # pragma: no cover - runs in the forked child
        status = 0
        try:
            wire.call("agent.list", {})
            os.write(write_fd, f"{wire._prefix}".encode())  # pyright: ignore[reportPrivateUsage] - the internal is what this test pins
        except BaseException:  # noqa: BLE001 - any failure in the child becomes its exit status
            status = 1
        os._exit(status)

    os.close(write_fd)
    child_prefix = os.read(read_fd, 4096).decode()
    os.close(read_fd)
    _, wait_status = os.waitpid(child, 0)

    assert os.waitstatus_to_exitcode(wait_status) == 0, "the child could not call"
    assert child_prefix
    assert child_prefix != wire._prefix, "the child re-used the parent's id prefix"  # pyright: ignore[reportPrivateUsage] - the internal is what this test pins
    assert str(os.getpid()) not in child_prefix, "the child kept the parent's pid"
    assert server.connections >= 2, "the child re-used the parent's socket"

    wire.call("agent.list", {})  # closing in the child left the parent's fd alone
    # Parent call, child call, parent call; the subject is the id prefix asserted above.
    assert server.connections == 3
    ids = [r["id"] for r in server.requests]
    assert len(set(ids)) == len(ids) == 3


# --------------------------------------------------------------------------
# Malformed frames
# --------------------------------------------------------------------------


def test_a_non_json_frame_is_an_invalid_response(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    serve(sock_path, lambda _r, _c: b"this is not json\n", request)
    wire = transport(sock_path)
    with pytest.raises(InvalidResponseError):
        wire.call("session.snapshot", {})


def test_a_json_array_is_an_invalid_response(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    serve(sock_path, lambda _r, _c: b'["not", "an", "object"]\n', request)
    wire = transport(sock_path)
    with pytest.raises(InvalidResponseError) as exc:
        wire.call("session.snapshot", {})
    assert "list" in exc.value.message


def test_undecodable_pane_bytes_come_back_replaced_rather_than_raising(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """``UnicodeDecodeError`` is a ``ValueError``, not an ``OSError``."""

    def handler(req: dict[str, JsonValue], _c: socket.socket) -> bytes:
        head = json.dumps({"id": req["id"], "result": {"type": "ok", "text": ""}})
        return head[:-3].encode() + b"\xff\xfe" + head[-3:].encode() + b"\n"

    serve(sock_path, handler, request)
    wire = transport(sock_path)
    assert "\ufffd" in str(
        at(wire.call("agent.read", {"target": "w1:p3"}).payload, "result", "text")
    )


def test_undecodable_bytes_that_break_the_frame_are_an_invalid_response(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """The same replacement, landing outside a string, makes the JSON invalid."""
    serve(sock_path, lambda _r, _c: b'{"id": \xff\xfe}\n', request)
    wire = transport(sock_path)
    with pytest.raises(InvalidResponseError):
        wire.call("agent.read", {"target": "w1:p3"})


def test_a_frame_with_no_terminator_is_cut_off_rather_than_buffered_forever(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """A broken or hostile peer must not be able to grow the process heap."""

    def handler(_req: dict[str, JsonValue], conn: socket.socket) -> None:
        for _ in range(64):
            try:
                conn.sendall(b"x" * 4096)
            except OSError:
                return

    serve(sock_path, handler, request)
    # A small bound, so the endless line overruns it in two frames rather than thousands.
    wire = Tracked(sock_path, timeout=2.0, limits=replace(WIRE_LIMITS, max_frame_bytes=8192))
    with pytest.raises(InvalidResponseError) as exc:
        wire.call("session.snapshot", {})
    assert "terminator" in exc.value.message
    assert wire.still_open == []


# --------------------------------------------------------------------------
# The request bound
# --------------------------------------------------------------------------


def _pad_to(wire: SocketTransport, target: int) -> str:
    """The ``_pad`` value that makes this transport's next line exactly `target`."""
    frame = json.dumps(
        {
            "id": f"{wire._prefix}:1",  # pyright: ignore[reportPrivateUsage] - the internal is what this test pins
            "method": "session.snapshot",
            "params": {"_pad": ""},
        },
        separators=(",", ":"),
    )
    return "x" * (target - len(frame))


def test_a_request_of_exactly_the_limit_is_sent(server: FakeHerdr, sock_path: Path) -> None:
    """The boundary is inclusive, because that is where herdr's own is."""
    wire = SocketTransport(sock_path, timeout=FAST)
    wire.call("session.snapshot", {"_pad": _pad_to(wire, MAX_REQUEST_BYTES)})
    sent = json.dumps(server.requests[0], separators=(",", ":"))
    assert len(sent) == MAX_REQUEST_BYTES, "the fake did not receive the whole line"


def test_a_request_past_the_limit_is_refused_before_anything_is_sent(
    server: FakeHerdr, sock_path: Path
) -> None:
    """Nothing is written, no socket is opened, and the numbers are in the error."""
    wire = SocketTransport(sock_path, timeout=FAST)
    with pytest.raises(RequestTooLargeError) as caught:
        wire.call("session.snapshot", {"_pad": "x" * (MAX_REQUEST_BYTES + 1)})
    assert caught.value.limit == MAX_REQUEST_BYTES
    assert caught.value.size > MAX_REQUEST_BYTES
    assert str(caught.value.size) in caught.value.message
    assert server.connections == 0, "an over-large call must not cost a connection"
    assert server.requests == []


def test_the_refusal_is_still_an_invalid_request(sock_path: Path) -> None:
    """A route catching the taxonomy it already catches keeps working."""
    wire = SocketTransport(sock_path, timeout=FAST)
    with pytest.raises(InvalidRequestError):
        wire.call("session.snapshot", {"_pad": "x" * (MAX_REQUEST_BYTES + 1)})
    with pytest.raises(ValueError):  # noqa: PT011 - the type is the subject, not the message
        wire.call("session.snapshot", {"_pad": "x" * (MAX_REQUEST_BYTES + 1)})
    with pytest.raises(EngineError):
        wire.call("session.snapshot", {"_pad": "x" * (MAX_REQUEST_BYTES + 1)})


# --------------------------------------------------------------------------
# Id correlation
# --------------------------------------------------------------------------


def test_a_reply_for_an_id_nobody_awaits_is_skipped_with_a_warning(
    sock_path: Path, request: pytest.FixtureRequest, caplog: pytest.LogCaptureFixture
) -> None:
    """An unsolicited frame is stepped over, not mistaken for the answer."""

    def handler(req: dict[str, JsonValue], conn: socket.socket) -> bytes:
        stray: dict[str, JsonValue] = {"id": "somebody-else", "result": {"type": "ok"}}
        return _line(stray) + _line(ok(req, conn))

    serve(sock_path, handler, request)
    wire = transport(sock_path)
    with caplog.at_level(logging.WARNING):
        reply = wire.call("session.snapshot", {})
    assert at(reply.payload, "result", "echo") == "session.snapshot"
    assert "somebody-else" in caplog.text


def test_a_stream_that_only_answers_other_ids_is_declared_out_of_step(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    def handler(_req: dict[str, JsonValue], _conn: socket.socket) -> bytes:
        return _line({"id": "wrong", "result": {"type": "ok"}}) * (WIRE_LIMITS.max_stray_frames + 2)

    serve(sock_path, handler, request)
    wire = Tracked(sock_path)
    with pytest.raises(InvalidResponseError) as exc:
        wire.call("session.snapshot", {})
    assert "out of step" in exc.value.message
    assert wire.still_open == []


def test_a_late_reply_left_in_the_pipe_is_never_returned_to_the_next_caller(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """The nightmare case, staged: two frames, the first one stale."""

    def handler(req: dict[str, JsonValue], conn: socket.socket) -> bytes:
        if req["method"] == "session.snapshot":
            return _line(ok(req, conn)) + _line(
                {"id": "stale", "result": {"type": "ok", "echo": "stale"}}
            )
        return _line(ok(req, conn))

    serve(sock_path, handler, request)
    wire = transport(sock_path)
    wire.call("session.snapshot", {})
    assert at(wire.call("agent.list", {}).payload, "result", "echo") == "agent.list"


# --------------------------------------------------------------------------
# Reconnect, retry, backoff
# --------------------------------------------------------------------------


def test_a_dead_reused_connection_is_transparently_redialled(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """herdr restarting between two polls must not surface as an error."""

    def handler(req: dict[str, JsonValue], conn: socket.socket) -> bytes | None:
        if len(fake.requests) == 1:
            conn.sendall(_line(ok(req, conn)))
            conn.close()
            return None
        return _line(ok(req, conn))

    fake = serve(sock_path, handler, request)
    wire = transport(sock_path)
    wire.call("agent.list", {})
    assert at(wire.call("agent.list", {}).payload, "result", "echo") == "agent.list"
    assert fake.connections == 2


def test_a_write_is_never_resent(sock_path: Path, request: pytest.FixtureRequest) -> None:
    """``sendall`` can fail after the bytes have landed."""

    def handler(_req: dict[str, JsonValue], conn: socket.socket) -> bytes | None:
        conn.close()  # accepted, then gone: delivery is genuinely unknown
        return None

    fake = serve(sock_path, handler, request)
    wire = transport(sock_path)
    with pytest.raises(EngineUnavailableError):
        wire.call("agent.prompt", {"target": "w1:p3"}, idempotent=False)
    assert len(fake.requests) <= 1, "a write was sent more than once"


def test_a_fresh_connection_that_fails_is_not_retried(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """Retrying what just failed on a brand new socket buys nothing."""

    def handler(_req: dict[str, JsonValue], conn: socket.socket) -> None:
        conn.close()

    fake = serve(sock_path, handler, request)
    wire = transport(sock_path)
    with pytest.raises(EngineUnavailableError):
        wire.call("agent.list", {})
    assert len(fake.requests) == 1


class Clock:
    """A hand-wound monotonic clock, so backoff is asserted rather than slept."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> MonoTime:
        return MonoTime(self.now)


def test_connect_failures_back_off_instead_of_dialling_every_poll(
    tmp_path: Path,
) -> None:
    """A dead runner behind a 3s poll loop must not mean a syscall per tick."""
    clock = Clock()
    wire = transport(tmp_path / "gone.sock", clock=clock)
    with pytest.raises(EngineUnavailableError) as first:
        wire.call("session.snapshot", {})
    assert "not running" in first.value.message

    with pytest.raises(EngineUnavailableError) as gated:
        wire.call("session.snapshot", {})
    assert "not retrying" in gated.value.message

    clock.now += 10
    with pytest.raises(EngineUnavailableError) as after:
        wire.call("session.snapshot", {})
    assert "not running" in after.value.message


def test_the_backoff_is_bounded_and_a_success_clears_it(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    clock = Clock()
    # A cap of its own, so the bound shown is the one the transport was handed.
    wire = transport(sock_path, clock=clock, limits=replace(WIRE_LIMITS, backoff_cap=0.8))
    for _ in range(10):  # ten failed dials, doubling each time, well past the cap
        clock.now += 100
        with pytest.raises(EngineUnavailableError):
            wire.call("session.snapshot", {})
    assert wire._backoff == 0.8  # pyright: ignore[reportPrivateUsage] - the internal is what this test pins

    serve(sock_path, ok, request)
    clock.now += 100
    wire.call("session.snapshot", {})
    assert wire._backoff == 0.0  # pyright: ignore[reportPrivateUsage] - the internal is what this test pins
    assert wire._retry_at == 0.0  # pyright: ignore[reportPrivateUsage] - the internal is what this test pins


# --------------------------------------------------------------------------
# Threads
# --------------------------------------------------------------------------


def test_concurrent_callers_each_get_their_own_reply(server: FakeHerdr, sock_path: Path) -> None:
    """FastAPI's threadpool is the expected caller, so this is not academic."""
    wire = transport(sock_path, timeout=5.0)
    seen: list[tuple[str, JsonValue]] = []
    lock = threading.Lock()

    def ask(n: int) -> None:
        method = f"agent.get.{n}"
        reply = wire.call(method, {"target": str(n)})
        with lock:
            seen.append((method, at(reply.payload, "result", "echo")))

    threads = [threading.Thread(target=ask, args=(n,)) for n in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert len(seen) == 16
    assert all(asked == echoed for asked, echoed in seen), "a caller got somebody else's reply"
    # Sixteen connections, not one: herdr never allowed reuse; the pairing above is the subject.
    assert server.connections == 16


def test_calls_from_many_threads_are_in_flight_at_once(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """A slow read must not queue a key tap behind it: each call has its own connection."""
    callers = 4
    # Answers only once every caller is inside the runner at the same time.
    together = threading.Barrier(callers, timeout=2.0)

    def handler(req: dict[str, JsonValue], conn: socket.socket) -> dict[str, JsonValue] | None:
        try:
            together.wait()
        except threading.BrokenBarrierError:
            return None
        return ok(req, conn)

    serve(sock_path, handler, request)
    wire = transport(sock_path, timeout=3.0)
    outcomes: list[str] = []
    lock = threading.Lock()

    def ask(n: int) -> None:
        try:
            wire.call("agent.get", {"target": str(n)})
            outcome = "ok"
        except EngineError as exc:
            outcome = type(exc).__name__
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=ask, args=(n,)) for n in range(callers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert outcomes == ["ok"] * callers, "the calls were served one at a time"


# --------------------------------------------------------------------------
# The fake has to speak the protocol the runner speaks
# --------------------------------------------------------------------------

# Only ever the disposable hp-test session: never HERDR_SOCKET_PATH, the default session's socket.
LIVE_SOCKET = Path.home() / ".config/herdr/sessions/hp-test/herdr.sock"


def live_socket() -> Path | None:
    """The disposable session's socket, if one is actually listening."""
    path = LIVE_SOCKET
    if not path.exists():
        return None
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(1.0)
    try:
        probe.connect(str(path))
    except OSError:
        return None
    finally:
        probe.close()
    return path


def hang_up_behaviour(path: Path, timeout: float = 3.0) -> tuple[bool, str]:
    """Ask one question on a raw socket and report what the peer does next."""
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(timeout)
    try:
        conn.connect(str(path))
        conn.sendall(_line({"id": "probe-1", "method": "session.snapshot", "params": {}}))
        buffer = b""
        while b"\n" not in buffer:
            chunk = conn.recv(65536)
            if not chunk:
                raise AssertionError("the peer hung up without answering at all")
            buffer += chunk
        eof = conn.recv(65536) == b""
        try:
            conn.sendall(_line({"id": "probe-2", "method": "session.snapshot", "params": {}}))
        except OSError as exc:
            return eof, type(exc).__name__
        return eof, "accepted"
    finally:
        conn.close()


def test_the_fake_hangs_up_after_every_reply(server: FakeHerdr, sock_path: Path) -> None:
    """The fake's honesty, pinned as a fact rather than left as a comment."""
    eof, second = hang_up_behaviour(sock_path)
    assert eof, "the fake kept the connection open after replying; herdr does not"
    assert second == "BrokenPipeError", f"a second request was {second}, not refused"
    assert server.per_connection == [1], "one request per connection, no more"


def test_a_second_request_on_one_connection_is_refused(server: FakeHerdr, sock_path: Path) -> None:
    """The trap, disarmed: a reuse assumption now fails where it is written."""
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(2.0)
    try:
        conn.connect(str(sock_path))
        conn.sendall(_line({"id": "a", "method": "agent.list", "params": {}}))
        assert b"\n" in conn.recv(65536)

        def send_again() -> bool:
            # Raises once the hang-up has landed, which may be after the first send or later.
            conn.sendall(_line({"id": "b", "method": "agent.list", "params": {}}))
            return False

        with pytest.raises(OSError):  # noqa: PT011 - EPIPE or ECONNRESET, whichever lands first
            _ = until(send_again, HANG_UP_SECONDS)
    finally:
        conn.close()
    assert len(server.requests) == 1, "the second request reached the fake"


@pytest.mark.usefixtures("server")
def test_live_the_runner_hangs_up_exactly_like_the_fake(sock_path: Path) -> None:
    """The one live check in this module: does the fake still lie?"""
    path = live_socket()
    if path is None:
        pytest.skip(
            f"no live runner at {LIVE_SOCKET} (run `just live-session`); "
            "the rest of this module is offline"
        )

    live = hang_up_behaviour(path)
    mocked = hang_up_behaviour(sock_path)  # the same probe, against the fake

    assert live == (True, "BrokenPipeError"), (
        f"herdr changed its half of the protocol: {live}. If it now keeps "
        "connections alive, that is a real finding -- file it, and change the "
        "fake and the transport together, not one without the other."
    )
    assert mocked == live, f"the fake has drifted from the runner again: fake={mocked} live={live}"


def _line_of(path: Path, size: int, timeout: float = 15.0) -> str:
    """Send one padded ``session.snapshot`` of exactly `size` bytes; say what came back."""
    frame: dict[str, JsonValue] = {
        "id": f"probe-{size}",
        "method": "session.snapshot",
        "params": {},
    }
    bare = json.dumps({**frame, "_pad": ""}, separators=(",", ":"))
    frame["_pad"] = "x" * (size - len(bare))
    line = json.dumps(frame, separators=(",", ":"))
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(timeout)
    try:
        conn.connect(str(path))
        conn.sendall(line.encode() + b"\n")
        buffer = b""
        while b"\n" not in buffer:
            chunk = conn.recv(65536)
            if not chunk:
                return "closed"
            buffer += chunk
    except TimeoutError:
        return "hung"
    except OSError as exc:
        return type(exc).__name__
    else:
        return "answered"
    finally:
        conn.close()


def test_live_the_request_line_ceiling_is_still_where_it_was_measured() -> None:
    """The second live check: is :data:`MAX_REQUEST_BYTES` still true?"""
    path = live_socket()
    if path is None:
        pytest.skip(f"no live runner at {LIVE_SOCKET} (run `just live-session`)")
    at_limit = _line_of(path, MAX_REQUEST_BYTES)
    over = _line_of(path, MAX_REQUEST_BYTES + 1)
    assert (at_limit, over) in {
        ("answered", "ConnectionResetError"),
        ("answered", "BrokenPipeError"),
    }, (
        f"herdr moved the request-line ceiling: at {MAX_REQUEST_BYTES} it "
        f"{at_limit}, at one byte more it {over}. That is a real finding -- "
        "re-measure and change MAX_REQUEST_BYTES and its docstring together."
    )


# --------------------------------------------------------------------------
# Nothing in the package spawns a process
# --------------------------------------------------------------------------


def test_nothing_in_the_package_spawns_a_process() -> None:
    """The transport swap is only finished when the subprocess path is gone."""
    offenders = [
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if "__pycache__" not in path.parts
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith(("import subprocess", "from subprocess", "import shutil"))
    ]
    assert not offenders, f"these modules spawn processes again: {offenders}"


# --------------------------------------------------------------------------
# Descriptor exhaustion is inside the taxonomy, not through it
# --------------------------------------------------------------------------

# A child, so the descriptor exhaustion is genuine, disposable and never reaches a runner.
EXHAUST = """
import os, resource, sys
from herdr_pwa.engine import EngineError
from tests.engine_seam import SocketTransport

wire = SocketTransport(sys.argv[1], timeout=0.5)
soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, (min(soft, 128), hard))

held = []
while True:
    try:
        held.append(os.open(os.devnull, os.O_RDONLY))
    except OSError:
        break

try:
    wire.call("session.snapshot", {})
    outcome = "NO-ERROR"
except EngineError as exc:
    outcome = f"{type(exc).__name__}|{exc.code}|{wire._retry_at > 0}|{exc}"
except BaseException as exc:
    outcome = f"ESCAPED|{type(exc).__name__}|{exc}"

os.close(held.pop())
sys.stdout.write(outcome)
"""


def test_running_out_of_descriptors_is_unavailable_and_backs_off(
    tmp_path: Path,
) -> None:
    """``socket.socket()`` sat outside the try block, so ``EMFILE`` escaped."""
    done = subprocess.run(  # noqa: S603 - our own interpreter on a script in this file
        [sys.executable, "-c", textwrap.dedent(EXHAUST), str(tmp_path / "herdr.sock")],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    parts = done.stdout.split("|")
    assert parts[0] == "EngineUnavailableError", f"the taxonomy was escaped: {done.stdout}"
    assert parts[1] == "engine_unavailable"
    assert parts[2] == "True", "the backoff was skipped, so it will re-dial every tick"
    assert "file descriptors" in parts[3], parts[3]


# --------------------------------------------------------------------------
# The stream
# --------------------------------------------------------------------------


def streaming(*events: dict[str, JsonValue], then_hang_up: bool = False) -> Handler:
    """A runner that confirms the subscription and then pushes, keeping the connection."""

    def handler(request: dict[str, JsonValue], conn: socket.socket) -> None:
        conn.sendall(_line({"id": request["id"], "result": {"type": "subscription_started"}}))
        for event in events:
            conn.sendall(_line(event))
        if then_hang_up:
            conn.close()
        # never answered, so the connection is left open

    return handler


def status(pane: str, state: str) -> dict[str, JsonValue]:
    """One frame in the shape the live runner was measured sending."""
    return {
        "event": "pane.agent_status_changed",
        "data": {
            "agent": "claude",
            "agent_status": state,
            "pane_id": pane,
            "workspace_id": pane.split(":", maxsplit=1)[0],
        },
    }


def stream(
    path: Path,
    *,
    timeout: float = FAST,
    subscriptions: Sequence[Mapping[str, JsonValue]] = ({"type": "pane.updated"},),
) -> EventStream:
    return EventStream(path, subscriptions=subscriptions, timeout=timeout)


def test_the_subscribe_is_one_frame_naming_every_subscription(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """The whole set goes in one request, because the runner takes no second one."""
    fake = serve(sock_path, streaming(status("w1:p1", "done")), request)
    subs = [
        {"type": "pane.agent_status_changed", "pane_id": "w1:p1"},
        {"type": "pane.agent_status_changed", "pane_id": "w2:p3"},
    ]
    with contextlib.closing(stream(sock_path, subscriptions=subs)) as live:
        assert at(next(iter(live)), "data", "pane_id") == "w1:p1"
    assert fake.methods == [EVENT_SUBSCRIBE]
    assert at(fake.requests[0], "params", "subscriptions") == subs


def test_events_arrive_in_order_and_carry_no_id(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """A subscription frame is not a reply, which is why it cannot be correlated."""
    serve(
        sock_path,
        streaming(status("w1:p1", "working"), status("w1:p1", "done")),
        request,
    )
    got: list[dict[str, JsonValue]] = []
    with contextlib.closing(stream(sock_path)) as live:
        for event in live:
            got.append(event)
            if len(got) == 2:
                break
    assert [at(one, "data", "agent_status") for one in got] == ["working", "done"]
    assert all("id" not in one for one in got)


def test_the_connection_is_written_to_exactly_once(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """Measured on herdr 0.8.0: a second frame on a subscribed connection is a hang-up."""
    fake = serve(sock_path, streaming(status("w1:p1", "done")), request)
    with contextlib.closing(stream(sock_path)) as live:
        next(iter(live))
    assert fake.per_connection == [1]


def test_a_frame_that_is_not_an_event_is_skipped_rather_than_yielded(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """Only `event` frames are the subscription's; anything else is somebody's reply."""
    serve(
        sock_path,
        streaming({"id": "someone-else", "result": {"type": "ok"}}, status("w1:p1", "done")),
        request,
    )
    with contextlib.closing(stream(sock_path)) as live:
        assert at(next(iter(live)), "data", "agent_status") == "done"


def test_the_runner_hanging_up_is_unavailable_rather_than_a_quiet_end(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """A stream that just stops must be a failure the caller can back off from."""
    serve(sock_path, streaming(status("w1:p1", "done"), then_hang_up=True), request)
    live = stream(sock_path)
    with pytest.raises(EngineUnavailableError, match="closed the event connection"):
        list(live)
    assert not live.closed, "nobody closed this: it was closed on us"


def test_an_error_frame_on_the_stream_maps_onto_the_taxonomy(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """The runner refuses a bad subscription by frame, and it must not read as a hang-up."""

    def handler(req: dict[str, JsonValue], _conn: socket.socket) -> dict[str, JsonValue]:
        return {
            "id": req["id"],
            "error": {"code": "pane_not_found", "message": "no such pane"},
        }

    serve(sock_path, handler, request)
    with pytest.raises(TargetNotFoundError, match="no such pane"):
        list(stream(sock_path))


def test_an_error_body_the_schema_does_not_describe_refuses_the_subscription(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    def handler(req: dict[str, JsonValue], _conn: socket.socket) -> dict[str, JsonValue]:
        return {"id": req["id"], "error": {"message": 3}}

    serve(sock_path, handler, request)
    with pytest.raises(EngineError) as exc:
        list(stream(sock_path))
    assert type(exc.value) is EngineError
    assert exc.value.code == "engine_error"
    assert exc.value.message.startswith("the runner reported ")


def test_a_reply_the_schema_rejects_never_confirms_the_subscription(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """Only a frame herdr's schema reads as ``subscription_started`` opens the stream."""

    def handler(req: dict[str, JsonValue], conn: socket.socket) -> None:
        for _ in range(WIRE_LIMITS.max_stray_frames):
            conn.sendall(_line({"id": req["id"], "result": {"type": "no_such_arm"}}))

    serve(sock_path, handler, request)
    with pytest.raises(InvalidResponseError, match="never confirmed the subscription"):
        list(stream(sock_path))


def test_a_stream_reads_only_as_many_stray_frames_as_its_limits_allow(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    def handler(req: dict[str, JsonValue], conn: socket.socket) -> None:
        conn.sendall(_line({"id": req["id"], "result": {"type": "no_such_arm"}}))
        conn.sendall(_line({"id": req["id"], "result": {"type": "subscription_started"}}))
        # A stream that read past its limit ends on this hang-up rather than waiting for events.
        conn.close()

    serve(sock_path, handler, request)
    one = EventStream(
        sock_path,
        subscriptions=[{"type": "pane.agent_status_changed", "pane_id": "w1:p1"}],
        timeout=FAST,
        limits=replace(WIRE_LIMITS, max_stray_frames=1),
    )
    with pytest.raises(InvalidResponseError, match="after 1 frames"):
        list(one)


def test_close_from_another_thread_ends_the_iteration(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """The loop blocks indefinitely on a quiet herd, so shutdown cannot rely on a timeout."""
    serve(sock_path, streaming(), request)
    live = stream(sock_path)
    threading.Timer(0.2, live.close).start()
    started = time.monotonic()
    with pytest.raises(EngineUnavailableError, match="was closed"):
        list(live)
    assert live.closed, "a caller-requested stop has to be distinguishable"
    assert time.monotonic() - started < FAST * 10


def test_a_subscription_with_nothing_in_it_is_refused_before_any_connection(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """It would hold a connection open forever and deliver nothing at all."""
    fake = serve(sock_path, streaming(), request)
    with pytest.raises(InvalidRequestError, match="never deliver"):
        list(stream(sock_path, subscriptions=[]))
    assert fake.connections == 0


def test_an_over_large_subscribe_costs_no_connection(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """The same bound the transport enforces, for the same measured reason."""
    fake = serve(sock_path, streaming(), request)
    huge = [
        {"type": "pane.agent_status_changed", "pane_id": "p" * 1000}
        for _ in range(MAX_REQUEST_BYTES // 900)
    ]
    with pytest.raises(RequestTooLargeError):
        list(stream(sock_path, subscriptions=huge))
    assert fake.connections == 0


def test_a_subscription_that_is_never_confirmed_times_out(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """Waiting forever for the handshake would hide a runner that took the request and stopped."""
    serve(sock_path, lambda _req, _conn: None, request)
    with pytest.raises(EngineTimeoutError, match="confirm the subscription"):
        list(stream(sock_path))


def test_a_missing_socket_is_unavailable_for_the_stream_too(tmp_path: Path) -> None:
    """The stream connects on its own, so it needs the same diagnosis, not an OSError."""
    with pytest.raises(EngineUnavailableError, match="not running"):
        list(stream(tmp_path / "nothing.sock"))


def test_a_refused_subscription_gives_the_descriptor_back(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """A refusal is a frame, not a hang-up, so nothing else closes the connection."""

    def handler(req: dict[str, JsonValue], _c: socket.socket) -> dict[str, JsonValue]:
        return {"id": req["id"], "error": {"code": "pane_not_found", "message": "gone"}}

    serve(sock_path, handler, request)
    before = open_fds()
    for _ in range(20):
        with pytest.raises(TargetNotFoundError):
            list(stream(sock_path))
    # The fake's own end closes on its thread, after the client is done; wait for it, not a leak.
    settled = eventually(lambda: open_fds() <= before, 5.0)
    assert settled, f"a refused subscription leaked its connection: {open_fds()} > {before}"


# --------------------------------------------------------------------------
# Before the send, or after it: whether a failed write may have run
# --------------------------------------------------------------------------


def _hang_up(_req: dict[str, JsonValue], conn: socket.socket) -> None:
    conn.close()


def _garbled(_req: dict[str, JsonValue], _conn: socket.socket) -> bytes:
    return b"this is not json\n"


def _silent(_req: dict[str, JsonValue], _conn: socket.socket) -> None:
    return None


@pytest.mark.parametrize(
    ("handler", "kind"),
    [
        (_hang_up, EngineUnavailableError),
        (_garbled, InvalidResponseError),
        (_silent, EngineTimeoutError),
    ],
)
def test_a_write_whose_answer_is_lost_is_marked_as_sent(
    sock_path: Path,
    request: pytest.FixtureRequest,
    handler: Handler,
    kind: type[EngineError],
) -> None:
    """The runner had the whole request when its answer went missing; it may have acted."""
    fake = serve(sock_path, handler, request)
    wire = transport(sock_path, timeout=0.2)
    with pytest.raises(kind) as exc:
        wire.call("agent.send_keys", {"target": "w1:p3", "keys": ["1"]}, idempotent=False)
    assert exc.value.after_send
    assert len(fake.requests) == 1


@pytest.mark.parametrize("handler", [_hang_up, _garbled, _silent])
def test_a_read_whose_answer_is_lost_is_not_marked(
    sock_path: Path, request: pytest.FixtureRequest, handler: Handler
) -> None:
    """A lost answer to a read did nothing, so a check before a write never reads as the write."""
    serve(sock_path, handler, request)
    wire = transport(sock_path, timeout=0.2)
    with pytest.raises(EngineError) as exc:
        wire.call("agent.get", {"target": "w1:p3"})
    assert not exc.value.after_send


def test_a_write_that_never_left_is_not_marked(tmp_path: Path) -> None:
    wire = transport(tmp_path / "nobody-here.sock")
    with pytest.raises(EngineUnavailableError) as exc:
        wire.call("agent.send_keys", {"target": "w1:p3", "keys": ["1"]}, idempotent=False)
    assert not exc.value.after_send


def test_a_write_too_large_to_send_is_not_marked(server: FakeHerdr, sock_path: Path) -> None:
    wire = transport(sock_path)
    with pytest.raises(RequestTooLargeError) as exc:
        wire.call("agent.prompt", {"text": "x" * MAX_REQUEST_BYTES}, idempotent=False)
    assert not exc.value.after_send
    assert server.requests == []


def test_a_write_the_runner_answered_with_an_error_is_not_marked(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """An error frame is the runner saying what it did not do, not a lost answer."""

    def handler(req: dict[str, JsonValue], _conn: socket.socket) -> dict[str, JsonValue]:
        return {"id": req["id"], "error": {"code": "agent_not_found", "message": "gone"}}

    serve(sock_path, handler, request)
    wire = transport(sock_path)
    with pytest.raises(TargetNotFoundError) as exc:
        wire.call("agent.send_keys", {"target": "w1:p3", "keys": ["1"]}, idempotent=False)
    assert not exc.value.after_send
