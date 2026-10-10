"""Transport tests, against a real AF_UNIX server rather than a mock.

Nothing here needs herdr running, and nothing here touches the network: each
test stands up a throwaway socket server in ``tmp_path`` that speaks the actual
protocol -- newline-delimited JSON objects -- so the framing, the id
correlation, the timeouts and the buffer bound are genuinely exercised. A
hand-rolled mock would have proved that the code calls the methods it calls; a
socket proves that the bytes are right.

The server is deliberately hostile on request: it can answer late, answer
garbage, answer somebody else's id, answer without a newline, hang up mid-flight
or never answer at all. Every one of those is a way a real runner can fail, and
every one of them has to come out of :meth:`SocketTransport.call` as an
:class:`EngineError` -- never as an ``OSError``, a ``ValueError`` or a
``json.JSONDecodeError``.
"""

import contextlib
import inspect
import json
import logging
import os
import socket
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from herdr_pwa.engine.transport import (
    EVENT_SUBSCRIBE,
    MAX_REQUEST_BYTES,
    MAX_STRAY_FRAMES,
    EngineError,
    EngineTimeout,
    EngineUnavailable,
    EventStream,
    InvalidRequest,
    InvalidResponse,
    RequestTooLarge,
    SocketTransport,
    TargetNotFound,
    Transport,
)

SRC = Path(__file__).resolve().parent.parent / "src" / "herdr_pwa"

#: Short enough that a timeout test is not a coffee break, long enough that a
#: loaded CI box does not fail a healthy round trip.
FAST = 0.4

Handler = Callable[[dict[str, Any], socket.socket], Any]


def open_fds() -> int:
    """Descriptors this process holds open. Linux-only, like the runner."""
    return len(os.listdir(f"/proc/{os.getpid()}/fd"))


def ok(request: dict[str, Any], _conn: socket.socket) -> dict[str, Any]:
    """The minimal well-formed reply: right id, an ``ok`` result arm."""
    return {"id": request["id"], "result": {"type": "ok", "echo": request["method"]}}


class FakeHerdr:
    """A throwaway runner: binds, accepts, answers once, and hangs up.

    ``handler(request, conn)`` returns a dict (sent as one JSON line), raw bytes
    (sent verbatim, so a test can send half a frame or two frames at once), or
    ``None`` to say nothing at all. It may also close ``conn`` itself, which is
    how "the runner went away mid-conversation" is staged.

    **One request per connection.** Once the handler has answered, the
    connection is closed, because that is what herdr 0.8.0 does -- measured on a
    live socket, a second request written to an already-answered connection
    raises ``EPIPE``. This fake used to serve as many requests on one connection
    as a test cared to send, and that single act of politeness is why the reuse bug
    survived the entire project: five tests asserted connection *reuse* as a
    desirable property, one of them named ``test_the_connection_is_long_lived``,
    and the suite certified a protocol the runner does not speak while every
    prompt from the user's phone died on a broken pipe. Reads limped along by
    retrying; writes are forbidden from retrying and so simply never worked.

    So this is not a detail to relax for the convenience of a future test. A
    test written on the assumption that a connection can be reused must fail
    here, on a developer's machine, in milliseconds -- rather than in
    production, six months later, on somebody's phone. If a reply is never sent
    (``handler`` returns ``None``) the connection is *left* open: that is how a
    runner that hangs, dribbles or answers late is staged, and none of those are
    reuse.
    """

    def __init__(self, path: Path, handler: Handler = ok) -> None:
        self.path = path
        self.handler = handler
        self.requests: list[dict[str, Any]] = []
        self.connections = 0
        #: Requests received per accepted connection, in accept order. Honest
        #: by construction: every entry is 1 unless the peer sent a second
        #: request into a connection that had already been answered and closed.
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
                conn, _ = self._server.accept()
            except (TimeoutError, OSError):
                continue
            self.connections += 1
            self.per_connection.append(0)
            index = len(self.per_connection) - 1
            threading.Thread(
                target=self._session, args=(conn, index), daemon=True
            ).start()

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
                    request = json.loads(line)
                    self.requests.append(request)
                    self.per_connection[index] += 1
                    if not self._answer(conn, request):
                        return

    def _answer(self, conn: socket.socket, request: dict[str, Any]) -> bool:
        """Answer once, then hang up. Returns False to end the session.

        The hang-up is the whole point: herdr closes after every
        reply, so anything written into this connection afterwards must fail
        the way it fails in production. A handler that declines to answer
        (``None``) leaves the connection open, because "never answered" and
        "answered and stayed open" are different runners and only the second
        one is a lie.
        """
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
    def methods(self) -> list[str]:
        return [r["method"] for r in self.requests]


def _line(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode() + b"\n"


@pytest.fixture
def sock_path(tmp_path: Path) -> Path:
    return tmp_path / "herdr.sock"


@pytest.fixture
def server(sock_path: Path):
    """A healthy runner. Tests that want a hostile one build their own."""
    fake = FakeHerdr(sock_path)
    yield fake
    fake.close()


def serve(path: Path, handler: Handler, request: pytest.FixtureRequest) -> FakeHerdr:
    fake = FakeHerdr(path, handler)
    request.addfinalizer(fake.close)
    return fake


def transport(path: Path, **kwargs: Any) -> SocketTransport:
    kwargs.setdefault("timeout", FAST)
    return SocketTransport(path, **kwargs)


# --------------------------------------------------------------------------
# The happy path, and the framing itself
# --------------------------------------------------------------------------


def test_the_request_is_one_newline_terminated_json_object(
    server: FakeHerdr, sock_path: Path
) -> None:
    """The whole protocol, asserted on the bytes rather than on an intention."""
    wire = transport(sock_path)
    reply = wire.call("session.snapshot", {})
    assert reply["result"]["echo"] == "session.snapshot"
    assert server.requests[0]["method"] == "session.snapshot"
    assert server.requests[0]["params"] == {}
    assert set(server.requests[0]) == {"id", "method", "params"}
    wire.close()


def test_params_are_passed_through_unmangled(
    server: FakeHerdr, sock_path: Path
) -> None:
    """Named params, so no argv escaping question: dashes, newlines, unicode."""
    wire = transport(sock_path)
    params = {"target": "w1:p3", "text": "--not-a-flag\nsecond line ✳"}
    wire.call("agent.prompt", params, idempotent=False)
    assert server.requests[0]["params"] == params
    wire.close()


def test_ids_are_unique(server: FakeHerdr, sock_path: Path) -> None:
    wire = transport(sock_path)
    for _ in range(5):
        wire.call("agent.list", {})
    ids = [r["id"] for r in server.requests]
    assert len(set(ids)) == 5
    assert all(str(os.getpid()) in i for i in ids)
    wire.close()


def test_a_reply_is_matched_to_the_id_that_asked_for_it(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """Deleting the id check must break this, and it did not used to.

    The old version of this test let a healthy server answer every request with
    the right id, so ``payload.get("id") == request_id`` was never false and the
    whole check could be deleted with the test still green -- it proved the
    happy path, which is the one case correlation does not matter in.

    Here the server answers every request with the id of the *first* one it ever
    saw. The first call is correct by construction; the second is handed a frame
    addressed to somebody else. Without the check that stale frame is returned as
    this call's answer -- the "somebody else's agent rendered as mine" failure.
    With it, the frame is skipped and, since the runner hangs up after its one
    reply, the call ends as :class:`EngineUnavailable`.

    That the failure is "closed while answering" rather than a timeout is the
    honest fake talking: it used to hold the connection open after replying, so
    the skip was followed by silence and the call ran out its budget. The
    subject is unchanged and still bites -- delete the ``id == request_id``
    check and the second call returns the first call's frame, raising nothing.
    """
    first: list[str] = []

    def handler(req: dict[str, Any], conn: socket.socket) -> dict[str, Any]:
        if not first:
            first.append(req["id"])
        return {"id": first[0], "result": {"type": "ok", "echo": req["method"]}}

    serve(sock_path, handler, request)
    wire = transport(sock_path, timeout=0.3)
    assert wire.call("session.snapshot", {})["result"]["echo"] == "session.snapshot"
    with pytest.raises(EngineError):  # never a reply, and never the wrong one
        wire.call("agent.list", {})  # answered under the first call's id
    wire.close()


def test_two_transports_never_mint_the_same_id(
    server: FakeHerdr, sock_path: Path
) -> None:
    """Ids are per instance, so two clients on one runner cannot collide."""
    one, two = transport(sock_path), transport(sock_path)
    one.call("agent.list", {})
    two.call("agent.list", {})
    assert server.requests[0]["id"] != server.requests[1]["id"]
    one.close()
    two.close()


def test_each_call_gets_its_own_connection(server: FakeHerdr, sock_path: Path) -> None:
    """One request per connection, because that is what herdr speaks.

    This test used to assert the opposite -- ten calls over one connection --
    and it passed against a fake that was happy to keep it open. The real
    server is not: measured on a live socket, a second request on the same
    connection raises EPIPE, so herdr closes after every reply.

    Reusing it cost a silent 2x on reads (EPIPE, drop, reconnect, succeed --
    invisible because it worked) and made writes IMPOSSIBLE, since a write must
    never be re-sent and so had no second attempt to be saved by. Every prompt
    sent from the phone died with "could not be sent: [Errno 32] Broken pipe".
    """
    wire = transport(sock_path)
    for _ in range(10):
        wire.call("agent.list", {})
    assert server.connections == 10
    wire.close()


def test_close_is_idempotent_and_the_transport_redials_after_it(
    server: FakeHerdr, sock_path: Path
) -> None:
    wire = transport(sock_path)
    wire.call("agent.list", {})
    wire.close()
    wire.close()
    wire.call("agent.list", {})
    assert server.connections == 2
    wire.close()


class NameOnly:
    """Everything the Protocol's ``isinstance`` looks at, and nothing it means."""

    def call(self) -> None:  # wrong signature, no reply, no wire
        pass

    def close(self) -> None:
        pass


def test_satisfies_the_transport_protocol(server: FakeHerdr, sock_path: Path) -> None:
    """``isinstance`` on a ``@runtime_checkable`` Protocol is not a conformance test.

    It checks that the attribute *names* exist and nothing else -- no signature,
    no return type, no behaviour. :class:`NameOnly` above passes it with both
    methods stubbed to ``pass``, which is asserted here rather than assumed, and
    is why the old one-line version of this test could not fail.

    So conformance is checked three ways: the names (which is all the old test
    did), the signatures against the Protocol's own declarations, and then the
    thing that actually matters -- driving a real transport through a
    ``Transport``-typed binding and getting the reply back, plus the idempotent
    ``close`` the Protocol's docstring promises.
    """
    assert isinstance(NameOnly(), Transport), "isinstance only checks names"
    for name in ("call", "close"):
        assert inspect.signature(getattr(SocketTransport, name)) == inspect.signature(
            getattr(Transport, name)
        ), f"{name} does not have the signature the Protocol declares"

    wire: Transport = transport(sock_path)
    reply = wire.call("session.snapshot", {}, timeout=FAST, idempotent=True)
    assert reply["result"]["echo"] == "session.snapshot"
    wire.close()
    wire.close()  # "calling it twice must be harmless"


# --------------------------------------------------------------------------
# Errors the runner reports. These are not transport failures.
# --------------------------------------------------------------------------


def test_an_error_frame_maps_onto_the_taxonomy(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """Pinned to the envelope herdr 0.8.0 actually sends."""

    def handler(req: dict[str, Any], _c: socket.socket) -> dict[str, Any]:
        return {
            "id": req["id"],
            "error": {
                "code": "agent_not_found",
                "message": "agent target nosuch not found",
            },
        }

    serve(sock_path, handler, request)
    wire = transport(sock_path)
    with pytest.raises(TargetNotFound) as exc:
        wire.call("agent.get", {"target": "nosuch"})
    assert exc.value.code == "agent_not_found"
    assert "nosuch" in exc.value.message
    wire.close()


def test_a_server_side_unavailable_code_is_not_a_transport_failure(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    def handler(req: dict[str, Any], _c: socket.socket) -> dict[str, Any]:
        return {
            "id": req["id"],
            "error": {"code": "server_not_running", "message": "no server"},
        }

    serve(sock_path, handler, request)
    wire = transport(sock_path)
    with pytest.raises(EngineUnavailable):
        wire.call("session.snapshot", {})
    wire.close()


def test_an_unknown_error_code_stays_a_plain_engine_error(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    def handler(req: dict[str, Any], _c: socket.socket) -> dict[str, Any]:
        return {"id": req["id"], "error": {"code": "weird_new_code", "message": "hm"}}

    serve(sock_path, handler, request)
    wire = transport(sock_path)
    with pytest.raises(EngineError) as exc:
        wire.call("agent.get", {"target": "w1:p3"})
    assert type(exc.value) is EngineError
    assert exc.value.code == "weird_new_code"
    wire.close()


def test_an_error_frame_is_a_conversation_not_a_failure(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """A refused request must not poison the next one.

    This used to assert the connection stayed open afterwards, on the reasoning
    that dropping it turns a poll over a departed pane into a reconnect per
    tick. The reasoning was sound and the premise was not -- herdr closes after
    every reply regardless, so the reconnect-per-tick is simply what the
    protocol costs. What still matters, and is what this now checks, is that an
    error frame is delivered as a typed error and the transport keeps working.
    """
    state = {"n": 0}

    def handler(req: dict[str, Any], _c: socket.socket) -> dict[str, Any]:
        state["n"] += 1
        if state["n"] == 1:
            return {
                "id": req["id"],
                "error": {"code": "pane_not_found", "message": "gone"},
            }
        return ok(req, _c)

    fake = serve(sock_path, handler, request)
    wire = transport(sock_path)
    with pytest.raises(TargetNotFound):
        wire.call("agent.get", {"target": "gone"})
    # The next call succeeds: an error frame did not poison anything. Each
    # gets its own connection, which is the protocol rather than a symptom.
    wire.call("agent.get", {"target": "here"})
    assert fake.connections == 2
    wire.close()


# --------------------------------------------------------------------------
# "herdr isn't there" — three shapes, one error class
# --------------------------------------------------------------------------


def test_a_missing_socket_is_unavailable_not_a_filenotfounderror(
    tmp_path: Path,
) -> None:
    wire = transport(tmp_path / "nothing-here.sock")
    with pytest.raises(EngineUnavailable) as exc:
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
        with pytest.raises(EngineUnavailable) as exc:
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
    with pytest.raises(EngineUnavailable):
        wire.call("session.snapshot", {})


def test_the_runner_hanging_up_mid_answer_is_unavailable(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    def handler(_req: dict[str, Any], conn: socket.socket) -> None:
        conn.close()
        return None

    serve(sock_path, handler, request)
    wire = transport(sock_path)
    with pytest.raises(EngineUnavailable) as exc:
        wire.call("session.snapshot", {})
    assert "closed the connection" in exc.value.message
    wire.close()


# --------------------------------------------------------------------------
# Timeouts bound the call, not the syscall
# --------------------------------------------------------------------------


def test_a_runner_that_never_answers_times_out(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    serve(sock_path, lambda _r, _c: None, request)
    wire = transport(sock_path, timeout=0.2)
    started = time.monotonic()
    with pytest.raises(EngineTimeout):
        wire.call("session.snapshot", {})
    assert time.monotonic() - started < 2.0
    wire.close()


def test_a_dribbling_peer_cannot_outlast_the_deadline(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """The bound is on the whole read, not on each ``recv``.

    A peer that sends one byte every 50ms resets a per-syscall timeout forever.
    This is why the socket timeout is re-armed from the *remaining* budget.
    """

    def handler(_req: dict[str, Any], conn: socket.socket) -> None:
        for _ in range(200):
            try:
                conn.sendall(b" ")
            except OSError:
                return
            time.sleep(0.02)
        return None

    serve(sock_path, handler, request)
    wire = transport(sock_path, timeout=0.3)
    started = time.monotonic()
    with pytest.raises(EngineTimeout):
        wire.call("session.snapshot", {})
    assert time.monotonic() - started < 2.0
    wire.close()


def test_a_timeout_destroys_the_connection(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """Otherwise the late reply is handed to whoever asks next.

    The reply to a timed-out ``agent.get`` sitting unread in the pipe would be
    returned as the answer to the *next* question. Closing is the only way to
    guarantee that cannot happen.
    """
    late: list[dict[str, Any]] = []

    def handler(req: dict[str, Any], conn: socket.socket) -> None:
        time.sleep(0.35)
        with contextlib_suppress():
            conn.sendall(_line(ok(req, conn)))
        late.append(req)
        return None

    serve(sock_path, handler, request)
    wire = transport(sock_path, timeout=0.15)
    with pytest.raises(EngineTimeout):
        wire.call("agent.get", {"target": "w1:p3"})
    assert wire._sock is None
    wire.close()


class contextlib_suppress:
    """Tiny local ``suppress(OSError)`` so the fake server stays dependency-free."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: type | None, *_: object) -> bool:
        return exc_type is not None and issubclass(exc_type, OSError)


def test_an_exhausted_budget_times_out_before_dialling(sock_path: Path) -> None:
    """A zero timeout must not become an unbounded connect -- and must not leak.

    The socket used to be created *before* the budget was checked, and the check
    raises :class:`EngineTimeout`, which is neither ``TimeoutError`` nor
    ``OSError`` and so escaped both handlers around the connect. ``self._sock``
    was never assigned, so nothing could reclaim the descriptor: only refcounting
    closed it, and anything that retains the traceback (``logging.exception``, a
    stashed ``exc_info``, a ``raise ... from`` chain hung off a request object)
    keeps the frame -- and the socket -- alive. Retaining the exceptions here is
    what makes the leak observable: 5 -> 55 descriptors over 50 calls.
    """
    wire = transport(sock_path, timeout=0.0)
    with pytest.raises(EngineTimeout):
        wire.call("session.snapshot", {})

    before = open_fds()
    retained: list[EngineTimeout] = []  # what logging.exception would hold
    for _ in range(50):
        with pytest.raises(EngineTimeout) as caught:
            wire.call("session.snapshot", {})
        retained.append(caught.value)
    assert open_fds() == before
    assert len(retained) == 50  # the tracebacks are still alive at this point


def test_a_retry_shares_the_calls_deadline_instead_of_doubling_it(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """``timeout=T`` must mean T for the *call*, whatever it does inside.

    The deadline used to be computed per *attempt*, so a call that retried took
    2T while the error message still quoted T. Stacked through the client that
    made a ``prompt`` cost 5 x command_timeout, with the transport lock held for
    all of it. The deadline is now computed once, in ``_call_locked``, and both
    attempts share it.

    The retry that produced the bug armed only on a *reused* connection, and
    there is no reuse any more: each call dials, asks and is hung up
    on. So this now guards the property rather than reproducing the fault --
    two calls, the second unanswered, and the second must cost one budget. The
    day herdr keeps a connection alive and the retry comes back, the assertion
    below is already the right one.
    """
    answered: list[int] = []

    def handler(req: dict[str, Any], conn: socket.socket) -> Any:
        answered.append(1)
        return ok(req, conn) if len(answered) == 1 else None

    serve(sock_path, handler, request)
    budget = 0.2
    wire = transport(sock_path, timeout=budget)
    wire.call("agent.list", {})  # answered, and hung up on, like every call
    started = time.monotonic()  # the second one is never answered at all
    with pytest.raises(EngineTimeout) as caught:
        wire.call("agent.get", {"target": "w1:p3"})
    elapsed = time.monotonic() - started
    assert elapsed < budget * 1.5, f"one budget, not two: {elapsed:.3f}s"
    assert str(budget) in str(caught.value), "the message must name the real budget"
    wire.close()


@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded")
def test_a_forked_child_gets_its_own_ids_and_its_own_connection(
    server: FakeHerdr, sock_path: Path
) -> None:
    """A preforking server must not hand two workers one stream and one id space.

    ``default_client()`` is cached, so anything that warms it before the fork
    (``gunicorn --preload``) gave every child the same id prefix, the same
    counter *and* the same open socket. Two workers then interleaved frames on
    one connection and each other's replies passed the ``id == request_id``
    check -- one user's request answered with another agent's data. The id check
    is the last line of defence, so it has to hold across a fork.
    """
    wire = transport(sock_path)
    wire.call("agent.list", {})  # parent: prefix minted, socket open, counter used

    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:  # pragma: no cover - runs in the forked child
        status = 0
        try:
            wire.call("agent.list", {})
            os.write(write_fd, f"{wire._prefix}".encode())
        except BaseException:
            status = 1
        os._exit(status)

    os.close(write_fd)
    child_prefix = os.read(read_fd, 4096).decode()
    os.close(read_fd)
    _, wait_status = os.waitpid(child, 0)

    assert os.waitstatus_to_exitcode(wait_status) == 0, "the child could not call"
    assert child_prefix and child_prefix != wire._prefix, (
        "the child re-used the parent's id prefix"
    )
    assert str(os.getpid()) not in child_prefix, "the child kept the parent's pid"
    assert server.connections >= 2, "the child re-used the parent's socket"

    wire.call("agent.list", {})  # closing in the child left the parent's fd alone
    # Three connections, not two: parent call, child call, parent call.
    # The subject of this test is the request-id PREFIX asserted above --
    # a forked child must never mint ids that collide with its parent's,
    # which is what would let one process read the other's reply.
    assert server.connections == 3
    ids = [r["id"] for r in server.requests]
    assert len(set(ids)) == len(ids) == 3
    wire.close()


# --------------------------------------------------------------------------
# Malformed frames
# --------------------------------------------------------------------------


def test_a_non_json_frame_is_an_invalid_response(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    serve(sock_path, lambda _r, _c: b"this is not json\n", request)
    wire = transport(sock_path)
    with pytest.raises(InvalidResponse):
        wire.call("session.snapshot", {})
    wire.close()


def test_a_json_array_is_an_invalid_response(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    serve(sock_path, lambda _r, _c: b'["not", "an", "object"]\n', request)
    wire = transport(sock_path)
    with pytest.raises(InvalidResponse) as exc:
        wire.call("session.snapshot", {})
    assert "list" in exc.value.message
    wire.close()


def test_undecodable_pane_bytes_come_back_replaced_rather_than_raising(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """``UnicodeDecodeError`` is a ``ValueError``, not an ``OSError``.

    An earlier review caught exactly this escaping every ``except EngineError``
    in the app on the subprocess path. A pane that cats a binary file, or a
    viewport cut mid-glyph, produces it for real -- so the decode replaces
    rather than raises, and the mirror shows a replacement glyph instead of the
    dashboard showing a traceback.
    """

    def handler(req: dict[str, Any], _c: socket.socket) -> bytes:
        head = json.dumps({"id": req["id"], "result": {"type": "ok", "text": ""}})
        return head[:-3].encode() + b"\xff\xfe" + head[-3:].encode() + b"\n"

    serve(sock_path, handler, request)
    wire = transport(sock_path)
    assert "\ufffd" in wire.call("agent.read", {"target": "w1:p3"})["result"]["text"]
    wire.close()


def test_undecodable_bytes_that_break_the_frame_are_an_invalid_response(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """The same replacement, landing outside a string, makes the JSON invalid.

    Which is an :class:`InvalidResponse` like any other malformed frame -- the
    point being that neither path can raise ``UnicodeDecodeError``.
    """
    serve(sock_path, lambda _r, _c: b'{"id": \xff\xfe}\n', request)
    wire = transport(sock_path)
    with pytest.raises(InvalidResponse):
        wire.call("agent.read", {"target": "w1:p3"})
    wire.close()


def test_a_frame_with_no_terminator_is_cut_off_rather_than_buffered_forever(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """A broken or hostile peer must not be able to grow the process heap."""

    def handler(_req: dict[str, Any], conn: socket.socket) -> None:
        for _ in range(64):
            try:
                conn.sendall(b"x" * 4096)
            except OSError:
                return
        return None

    serve(sock_path, handler, request)
    wire = transport(sock_path, timeout=2.0, max_frame_bytes=8192)
    with pytest.raises(InvalidResponse) as exc:
        wire.call("session.snapshot", {})
    assert "terminator" in exc.value.message
    assert wire._buffer == b""
    wire.close()


# --------------------------------------------------------------------------
# The request bound
# --------------------------------------------------------------------------


def _pad_to(wire: SocketTransport, target: int) -> str:
    """The ``_pad`` value that makes this transport's next line exactly `target`."""
    frame = json.dumps(
        {
            "id": f"{wire._prefix}:1",
            "method": "session.snapshot",
            "params": {"_pad": ""},
        },
        separators=(",", ":"),
    )
    return "x" * (target - len(frame))


def test_a_request_of_exactly_the_limit_is_sent(
    server: FakeHerdr, sock_path: Path
) -> None:
    """The boundary is inclusive, because that is where herdr's own is.

    Measured against herdr 0.8.0 on 2026-08-20: a request line of exactly
    1 MiB is answered and one byte more is not
    (``evidence/request-line-probe.md``).
    """
    wire = SocketTransport(sock_path, timeout=FAST)
    wire.call("session.snapshot", {"_pad": _pad_to(wire, MAX_REQUEST_BYTES)})
    sent = json.dumps(server.requests[0], separators=(",", ":"))
    assert len(sent) == MAX_REQUEST_BYTES, "the fake did not receive the whole line"
    wire.close()


def test_a_request_past_the_limit_is_refused_before_anything_is_sent(
    server: FakeHerdr, sock_path: Path
) -> None:
    """Nothing is written, no socket is opened, and the numbers are in the error.

    This is the request-bound fix. Past this size herdr logs ``api request line is too
    large`` and closes the connection, which reaches a caller as
    :class:`EngineUnavailable` -- "the herd is not answering" -- for a herd that
    is answering perfectly well, at 503, which invites a retry that can only
    fail the same way forever.
    """
    wire = SocketTransport(sock_path, timeout=FAST)
    with pytest.raises(RequestTooLarge) as caught:
        wire.call("session.snapshot", {"_pad": "x" * (MAX_REQUEST_BYTES + 1)})
    assert caught.value.limit == MAX_REQUEST_BYTES
    assert caught.value.size > MAX_REQUEST_BYTES
    assert str(caught.value.size) in caught.value.message
    assert server.connections == 0, "an over-large call must not cost a connection"
    assert server.requests == []
    wire.close()


def test_the_refusal_is_still_an_invalid_request(sock_path: Path) -> None:
    """A route catching the taxonomy it already catches keeps working."""
    wire = SocketTransport(sock_path, timeout=FAST)
    with pytest.raises(InvalidRequest):
        wire.call("session.snapshot", {"_pad": "x" * (MAX_REQUEST_BYTES + 1)})
    with pytest.raises(ValueError):
        wire.call("session.snapshot", {"_pad": "x" * (MAX_REQUEST_BYTES + 1)})
    with pytest.raises(EngineError):
        wire.call("session.snapshot", {"_pad": "x" * (MAX_REQUEST_BYTES + 1)})
    wire.close()


# --------------------------------------------------------------------------
# Id correlation
# --------------------------------------------------------------------------


def test_a_reply_for_an_id_nobody_awaits_is_skipped_with_a_warning(
    sock_path: Path, request: pytest.FixtureRequest, caplog: pytest.LogCaptureFixture
) -> None:
    """An unsolicited frame is stepped over, not mistaken for the answer.

    This is what makes the push stream land on an already-correct
    transport: a frame that is not the reply we are waiting for is discarded,
    never returned.
    """

    def handler(req: dict[str, Any], conn: socket.socket) -> bytes:
        stray = {"id": "somebody-else", "result": {"type": "ok"}}
        return _line(stray) + _line(ok(req, conn))

    serve(sock_path, handler, request)
    wire = transport(sock_path)
    with caplog.at_level(logging.WARNING):
        reply = wire.call("session.snapshot", {})
    assert reply["result"]["echo"] == "session.snapshot"
    assert "somebody-else" in caplog.text
    wire.close()


def test_a_stream_that_only_answers_other_ids_is_declared_out_of_step(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    def handler(_req: dict[str, Any], _conn: socket.socket) -> bytes:
        return _line({"id": "wrong", "result": {"type": "ok"}}) * (MAX_STRAY_FRAMES + 2)

    serve(sock_path, handler, request)
    wire = transport(sock_path)
    with pytest.raises(InvalidResponse) as exc:
        wire.call("session.snapshot", {})
    assert "out of step" in exc.value.message
    assert wire._sock is None  # and the poisoned connection is gone
    wire.close()


def test_a_late_reply_left_in_the_pipe_is_never_returned_to_the_next_caller(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """The nightmare case, staged: two frames, the first one stale.

    Even if a stale frame survives into the next call's buffer, the id check
    means the caller gets its own answer -- verified by asking for a different
    method and checking the echo.
    """

    def handler(req: dict[str, Any], conn: socket.socket) -> bytes:
        if req["method"] == "session.snapshot":
            return _line(ok(req, conn)) + _line(
                {"id": "stale", "result": {"type": "ok", "echo": "stale"}}
            )
        return _line(ok(req, conn))

    serve(sock_path, handler, request)
    wire = transport(sock_path)
    wire.call("session.snapshot", {})
    assert wire.call("agent.list", {})["result"]["echo"] == "agent.list"
    wire.close()


# --------------------------------------------------------------------------
# Reconnect, retry, backoff
# --------------------------------------------------------------------------


def test_a_dead_reused_connection_is_transparently_redialled(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """herdr restarting between two polls must not surface as an error.

    The first call succeeds and the runner then hangs up. The second call finds
    a corpse, and because a *read* is safe to repeat it is retried once on a
    fresh connection.
    """

    def handler(req: dict[str, Any], conn: socket.socket) -> bytes | None:
        if len(fake.requests) == 1:
            conn.sendall(_line(ok(req, conn)))
            conn.close()
            return None
        return _line(ok(req, conn))

    fake = serve(sock_path, handler, request)
    wire = transport(sock_path)
    wire.call("agent.list", {})
    time.sleep(0.1)  # let the hang-up arrive
    assert wire.call("agent.list", {})["result"]["echo"] == "agent.list"
    assert fake.connections == 2
    wire.close()


def test_a_write_is_never_resent(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """``sendall`` can fail after the bytes have landed.

    A retried ``agent.prompt`` is a second prompt typed at a real agent, so a
    non-idempotent call fails instead of being repeated -- and the caller, who
    knows whether it was delivered, decides.

    The setup changed with the protocol: this used to poison a *reused*
    connection and watch the write refuse to retry on it. There is no reuse
    now, so the failure is staged where it actually happens -- the server
    accepts the connection and dies mid-exchange, which is exactly the case
    where the bytes may or may not have arrived. Exactly one request must
    reach the server.
    """

    def handler(req: dict[str, Any], conn: socket.socket) -> bytes | None:
        conn.close()  # accepted, then gone: delivery is genuinely unknown
        return None

    fake = serve(sock_path, handler, request)
    wire = transport(sock_path)
    with pytest.raises(EngineUnavailable):
        wire.call("agent.prompt", {"target": "w1:p3"}, idempotent=False)
    assert len(fake.requests) <= 1, "a write was sent more than once"
    wire.close()


def test_a_fresh_connection_that_fails_is_not_retried(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """Retrying what just failed on a brand new socket buys nothing.

    Asserted through the request count: one attempt, one request.
    """

    def handler(_req: dict[str, Any], conn: socket.socket) -> None:
        conn.close()
        return None

    fake = serve(sock_path, handler, request)
    wire = transport(sock_path)
    with pytest.raises(EngineUnavailable):
        wire.call("agent.list", {})
    assert len(fake.requests) == 1
    wire.close()


class Clock:
    """A hand-wound monotonic clock, so backoff is asserted rather than slept."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_connect_failures_back_off_instead_of_dialling_every_poll(
    tmp_path: Path,
) -> None:
    """A dead runner behind a 3s poll loop must not mean a syscall per tick."""
    clock = Clock()
    wire = transport(tmp_path / "gone.sock", clock=clock)
    with pytest.raises(EngineUnavailable) as first:
        wire.call("session.snapshot", {})
    assert "not running" in first.value.message

    with pytest.raises(EngineUnavailable) as gated:
        wire.call("session.snapshot", {})
    assert "not retrying" in gated.value.message

    clock.now += 10
    with pytest.raises(EngineUnavailable) as after:
        wire.call("session.snapshot", {})
    assert "not running" in after.value.message


def test_the_backoff_is_bounded_and_a_success_clears_it(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    clock = Clock()
    wire = transport(sock_path, clock=clock, backoff_cap=0.5)
    for _ in range(10):  # ten failed dials, doubling each time
        clock.now += 100
        with pytest.raises(EngineUnavailable):
            wire.call("session.snapshot", {})
    assert wire._backoff == 0.5

    serve(sock_path, ok, request)
    clock.now += 100
    wire.call("session.snapshot", {})
    assert wire._backoff == 0.0
    assert wire._retry_at == 0.0
    wire.close()


# --------------------------------------------------------------------------
# Threads
# --------------------------------------------------------------------------


def test_concurrent_callers_each_get_their_own_reply(
    server: FakeHerdr, sock_path: Path
) -> None:
    """FastAPI's threadpool is the expected caller, so this is not academic.

    The lock means one request is outstanding at a time; what this proves is
    that the *answers do not cross* -- every thread's echo is its own method.
    """
    wire = transport(sock_path, timeout=5.0)
    seen: list[tuple[str, str]] = []
    lock = threading.Lock()

    def ask(n: int) -> None:
        method = f"agent.get.{n}"
        reply = wire.call(method, {"target": str(n)})
        with lock:
            seen.append((method, reply["result"]["echo"]))

    threads = [threading.Thread(target=ask, args=(n,)) for n in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert len(seen) == 16
    assert all(asked == echoed for asked, echoed in seen), (
        "a caller got somebody else's reply"
    )
    # Sixteen connections, not one: the lock still serialises callers, and the
    # id correlation still has to hold -- what changed is that reuse was never
    # something herdr allowed. The pairing assertion above is the real subject
    # of this test and is unaffected.
    assert server.connections == 16
    wire.close()


# --------------------------------------------------------------------------
# The fake has to speak the protocol the runner speaks
# --------------------------------------------------------------------------

#: Where a real runner may be found for the one live check below.
#:
#: Deliberately NOT ``HERDR_SOCKET_PATH``. That variable points at whatever
#: session the developer happens to be sitting in -- in practice the default
#: one, holding their real working agents -- and a test suite has no business
#: dialling that. The check is aimed at the disposable ``hp-test`` session
#: by default, or at whatever ``HP_LIVE_SOCKET`` names, and skips when
#: neither is there. CI has no runner and must stay green.
LIVE_SOCKET_ENV = "HP_LIVE_SOCKET"
DEFAULT_LIVE_SOCKET = Path.home() / ".config/herdr/sessions/hp-test/herdr.sock"


def live_socket_path() -> Path:
    """Where a live runner would be, whether or not one is there."""
    override = os.environ.get(LIVE_SOCKET_ENV)
    return Path(override) if override else DEFAULT_LIVE_SOCKET


def live_socket() -> Path | None:
    """The disposable session's socket, if one is actually listening."""
    path = live_socket_path()
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
    """Ask one question on a raw socket and report what the peer does next.

    Returns ``(the peer sent EOF after its reply, what a second request did)``.
    No :class:`SocketTransport` is involved on purpose: the subject is the
    *server's* half of the protocol, and running it through the client under
    test would let a client that papers over the hang-up hide it.
    """
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(timeout)
    try:
        conn.connect(str(path))
        conn.sendall(
            _line({"id": "probe-1", "method": "session.snapshot", "params": {}})
        )
        buffer = b""
        while b"\n" not in buffer:
            chunk = conn.recv(65536)
            if not chunk:
                raise AssertionError("the peer hung up without answering at all")
            buffer += chunk
        eof = conn.recv(65536) == b""
        try:
            conn.sendall(
                _line({"id": "probe-2", "method": "session.snapshot", "params": {}})
            )
        except OSError as exc:
            return eof, type(exc).__name__
        return eof, "accepted"
    finally:
        conn.close()


def test_the_fake_hangs_up_after_every_reply(
    server: FakeHerdr, sock_path: Path
) -> None:
    """The fake's honesty, pinned as a fact rather than left as a comment.

    The connection-reuse bug was undetectable for the life of the project because this fake was
    happy to serve many requests on one connection while herdr closes after
    every reply. Making it close is the fix; asserting that it closes is what
    stops the fix from being quietly relaxed by whoever finds it inconvenient.
    """
    eof, second = hang_up_behaviour(sock_path)
    assert eof, "the fake kept the connection open after replying; herdr does not"
    assert second == "BrokenPipeError", f"a second request was {second}, not refused"
    assert server.per_connection == [1], "one request per connection, no more"


def test_a_second_request_on_one_connection_is_refused(
    server: FakeHerdr, sock_path: Path
) -> None:
    """The trap, disarmed: a reuse assumption now fails where it is written.

    Written as the shortest form of the mistake -- two calls on one socket --
    so that anybody who reintroduces it sees this test rather than a phone.
    """
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(2.0)
    try:
        conn.connect(str(sock_path))
        conn.sendall(_line({"id": "a", "method": "agent.list", "params": {}}))
        assert b"\n" in conn.recv(65536)
        with pytest.raises(OSError):
            for _ in range(3):  # the RST may land between the first and second
                conn.sendall(_line({"id": "b", "method": "agent.list", "params": {}}))
                time.sleep(0.05)
    finally:
        conn.close()
    assert len(server.requests) == 1, "the second request reached the fake"


def test_live_the_runner_hangs_up_exactly_like_the_fake(
    server: FakeHerdr, sock_path: Path
) -> None:
    """The one live check in this module: does the fake still lie?

    Everything else here is mocked and stays mocked -- fast, deterministic,
    offline, and the default. This single test exists because a mock can only
    be trusted for as long as somebody checks it against the thing it mocks,
    and nobody ever did: that is the entire mechanism of the reuse bug.

    It skips cleanly when no disposable runner is listening, which is the case
    in CI and on any machine that has not run ``just live-session``. It never
    dials the developer's own session -- see :data:`LIVE_SOCKET_ENV` -- and it
    asks one read-only ``session.snapshot``, so it drives no agent and spends
    no subscription tokens.
    """
    path = live_socket()
    if path is None:
        pytest.skip(
            f"no live runner at {live_socket_path()} (set {LIVE_SOCKET_ENV} or "
            "run `just live-session`); the rest of this module is offline"
        )

    live = hang_up_behaviour(path)
    mocked = hang_up_behaviour(sock_path)  # the same probe, against the fake

    assert live == (True, "BrokenPipeError"), (
        f"herdr changed its half of the protocol: {live}. If it now keeps "
        "connections alive, that is a real finding -- file it, and change the "
        "fake and the transport together, not one without the other."
    )
    assert mocked == live, (
        f"the fake has drifted from the runner again: fake={mocked} live={live}"
    )


def _line_of(path: Path, size: int, timeout: float = 15.0) -> str:
    """Send one padded ``session.snapshot`` of exactly `size` bytes; say what came back."""
    frame = {"id": f"probe-{size}", "method": "session.snapshot", "params": {}}
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
        return "answered"
    except TimeoutError:
        return "hung"
    except OSError as exc:
        return type(exc).__name__
    finally:
        conn.close()


def test_live_the_request_line_ceiling_is_still_where_it_was_measured(
    request: pytest.FixtureRequest,
) -> None:
    """The second live check: is :data:`MAX_REQUEST_BYTES` still true?

    A constant that records a measurement is only worth the last time somebody
    took it. This asks two read-only ``session.snapshot`` calls -- one at the
    limit, one a byte over -- on raw sockets, drives no agent, and skips
    cleanly with no disposable runner listening.

    Collie recorded this ceiling as "1 MiB or more is dropped". Neither half
    held here: 1 MiB exactly is answered, and past it herdr hangs up rather
    than going quiet. If this test starts failing, re-run
    ``evidence/probe.py`` and move the constant and its docstring's version
    together.
    """
    path = live_socket()
    if path is None:
        pytest.skip(
            f"no live runner at {live_socket_path()} (set {LIVE_SOCKET_ENV} or "
            "run `just live-session`)"
        )
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
# The claim the ticket is really about
# --------------------------------------------------------------------------


def test_nothing_in_the_package_spawns_a_process() -> None:
    """The transport swap is only finished when the subprocess path is gone.

    ``engine/client.py`` was the last importer of :mod:`subprocess` in the
    package; this fails if one creeps back in, whether as a fallback nobody
    removed or a shell-out added in a hurry.
    """
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

#: Run in a child so the descriptor exhaustion is genuine and disposable.
#:
#: Mocking ``socket.socket`` to raise would prove the handler and nothing about
#: reachability -- and reachability is the whole finding: ``EMFILE`` is exactly
#: the condition the old fd leak used to create, and the existing fd-leak test
#: counts descriptors rather than exhausting them, so it cannot see this. The
#: child drops ``RLIMIT_NOFILE``, opens every descriptor the limit allows, and
#: then makes one real call with nothing left to build a socket out of.
#:
#: Everything the child needs -- the import, the transport object -- is prepared
#: *before* the descriptors run out, and the result is written to stdout, whose
#: descriptor it already holds. It never reaches the runner; there is no runner.
EXHAUST = """
import os, resource, sys
from herdr_pwa.engine.transport import EngineError, SocketTransport

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
    """``socket.socket()`` sat outside the try block, so ``EMFILE`` escaped.

    The fd-leak fix moved the budget check above the constructor but left the
    constructor itself unguarded, and creating a descriptor is a syscall that
    fails. Under ``EMFILE`` the bare ``OSError(24, 'Too many open files')``
    walked through ``call()`` -> ``snapshot()`` -> the feed route and became a
    500 with a traceback, past every ``except EngineError`` in the app -- the one
    thing this module's docstring promises cannot happen.

    Worse than the 500: it skipped ``_penalise()``, so a process in this state
    re-dialled on every poll tick instead of backing off, which is precisely when
    a box under descriptor pressure least needs a syscall storm. Both halves are
    asserted.
    """
    done = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(EXHAUST), str(tmp_path / "herdr.sock")],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    parts = done.stdout.split("|")
    assert parts[0] == "EngineUnavailable", f"the taxonomy was escaped: {done.stdout}"
    assert parts[1] == "engine_unavailable"
    assert parts[2] == "True", "the backoff was skipped, so it will re-dial every tick"
    assert "file descriptors" in parts[3], parts[3]


# --------------------------------------------------------------------------
# The stream
# --------------------------------------------------------------------------
#
# @doc: docs/modules/engine/transport/EventStream.md


def streaming(*events: dict[str, Any], then_hang_up: bool = False) -> Handler:
    """A runner that confirms the subscription and then pushes, keeping the connection."""

    def handler(request: dict[str, Any], conn: socket.socket) -> None:
        conn.sendall(
            _line({"id": request["id"], "result": {"type": "subscription_started"}})
        )
        for event in events:
            conn.sendall(_line(event))
        if then_hang_up:
            conn.close()
        return None  # never answered, so the connection is left open

    return handler


def status(pane: str, state: str) -> dict[str, Any]:
    """One frame in the shape the live runner was measured sending."""
    return {
        "event": "pane.agent_status_changed",
        "data": {
            "agent": "claude",
            "agent_status": state,
            "pane_id": pane,
            "workspace_id": pane.split(":")[0],
        },
    }


def stream(path: Path, **kwargs: Any) -> EventStream:
    kwargs.setdefault("timeout", FAST)
    kwargs.setdefault("subscriptions", [{"type": "pane.updated"}])
    return EventStream(path, **kwargs)


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
        assert next(iter(live))["data"]["pane_id"] == "w1:p1"
    assert fake.methods == [EVENT_SUBSCRIBE]
    assert fake.requests[0]["params"]["subscriptions"] == subs


def test_events_arrive_in_order_and_carry_no_id(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """A subscription frame is not a reply, which is why it cannot be correlated."""
    serve(
        sock_path,
        streaming(status("w1:p1", "working"), status("w1:p1", "done")),
        request,
    )
    got = []
    with contextlib.closing(stream(sock_path)) as live:
        for event in live:
            got.append(event)
            if len(got) == 2:
                break
    assert [one["data"]["agent_status"] for one in got] == ["working", "done"]
    assert all("id" not in one for one in got)


def test_the_connection_is_written_to_exactly_once(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """Measured on herdr 0.8.0: a second frame on a subscribed connection is a hang-up.

    Not a style rule. `session.snapshot`, a second `events.subscribe` and even
    a bare `ping` each got the connection closed within ~50ms, so this class
    must never write again after the subscribe, and this pins that it does not.
    """
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
        streaming(
            {"id": "someone-else", "result": {"type": "ok"}}, status("w1:p1", "done")
        ),
        request,
    )
    with contextlib.closing(stream(sock_path)) as live:
        assert next(iter(live))["data"]["agent_status"] == "done"


def test_the_runner_hanging_up_is_unavailable_rather_than_a_quiet_end(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """A stream that just stops must be a failure the caller can back off from."""
    serve(sock_path, streaming(status("w1:p1", "done"), then_hang_up=True), request)
    live = stream(sock_path)
    with pytest.raises(EngineUnavailable, match="closed the event connection"):
        list(live)
    assert not live.closed, "nobody closed this: it was closed on us"


def test_an_error_frame_on_the_stream_maps_onto_the_taxonomy(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """The runner refuses a bad subscription by frame, and it must not read as a hang-up."""

    def handler(req: dict[str, Any], conn: socket.socket) -> dict[str, Any]:
        return {
            "id": req["id"],
            "error": {"code": "pane_not_found", "message": "no such pane"},
        }

    serve(sock_path, handler, request)
    with pytest.raises(TargetNotFound, match="no such pane"):
        list(stream(sock_path))


def test_close_from_another_thread_ends_the_iteration(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """The loop blocks indefinitely on a quiet herd, so shutdown cannot rely on a timeout."""
    serve(sock_path, streaming(), request)
    live = stream(sock_path)
    threading.Timer(0.2, live.close).start()
    started = time.monotonic()
    with pytest.raises(EngineUnavailable, match="was closed"):
        list(live)
    assert live.closed, "a caller-requested stop has to be distinguishable"
    assert time.monotonic() - started < FAST * 10


def test_a_subscription_with_nothing_in_it_is_refused_before_any_connection(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """It would hold a connection open forever and deliver nothing at all."""
    fake = serve(sock_path, streaming(), request)
    with pytest.raises(InvalidRequest, match="never deliver"):
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
    with pytest.raises(RequestTooLarge):
        list(stream(sock_path, subscriptions=huge))
    assert fake.connections == 0


def test_a_subscription_that_is_never_confirmed_times_out(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """Waiting forever for the handshake would hide a runner that took the request and stopped."""
    serve(sock_path, lambda _req, _conn: None, request)
    with pytest.raises(EngineTimeout, match="confirm the subscription"):
        list(stream(sock_path))


def test_a_missing_socket_is_unavailable_for_the_stream_too(tmp_path: Path) -> None:
    """The stream connects on its own, so it needs the same diagnosis, not an OSError."""
    with pytest.raises(EngineUnavailable, match="not running"):
        list(stream(tmp_path / "nothing.sock"))


def test_a_refused_subscription_gives_the_descriptor_back(
    sock_path: Path, request: pytest.FixtureRequest
) -> None:
    """A refusal is a frame, not a hang-up, so nothing else closes the connection.

    The subscribe happens inside `open`, which is called before `__iter__`'s
    try block ever exists -- so an error frame propagated with the socket still
    held. A notifier redialling a stream the runner keeps refusing would have
    leaked one descriptor per attempt, forever.
    """

    def handler(req: dict[str, Any], _c: socket.socket) -> dict[str, Any]:
        return {"id": req["id"], "error": {"code": "pane_not_found", "message": "gone"}}

    serve(sock_path, handler, request)
    before = open_fds()
    for _ in range(20):
        with pytest.raises(TargetNotFound):
            list(stream(sock_path))
    assert open_fds() <= before, "a refused subscription leaked its connection"
