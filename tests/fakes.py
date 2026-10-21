"""Test doubles: a fake tailscaled on a throwaway unix socket, a settable clock, a fake agent."""

import hashlib
import json
import socketserver
import threading
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from herdr_pwa.ids import PaneId, SessionId
from herdr_pwa.tailscale import SERVE_CONFIG_PATH, STATUS_PATH, LocalApi, serve_config

NODE = "host.tailnet.ts.net"
LOGIN = "operator@example.com"
GOOD_STATUS: dict[str, object] = {
    "BackendState": "Running",
    "Self": {"HostName": "host", "DNSName": f"{NODE}.", "UserID": 5886365405394843},
    # Go writes map[UserID] keys as strings while Self.UserID stays a number.
    "User": {"5886365405394843": {"LoginName": LOGIN, "DisplayName": "Op"}},
}


@dataclass
class Canned:
    """One fixed answer; ``raw`` bypasses HTTP framing, ``hang`` never answers, ``late`` stalls."""

    status: int = 200
    body: str = ""
    etag: str = ""
    raw: bytes | None = None
    hang: bool = False
    # Seconds the answer waits, as a loaded box would make it.
    late: float = 0.0


@dataclass
class Seen:
    """One request as the fake received it."""

    method: str
    path: str
    headers: dict[str, str]
    body: str


@dataclass
class FakeTailscaled:
    """Answers canned routes, and serve-config statefully with an ETag compare-and-swap."""

    socket_path: Path
    canned: dict[tuple[str, str], Canned] = field(default_factory=dict[tuple[str, str], Canned])
    serve_config: object = None
    post_status: int = 0
    requests: list[Seen] = field(default_factory=list[Seen])
    released: threading.Event = field(default_factory=threading.Event)

    @property
    def api(self) -> LocalApi:
        """A client pointed at this fake, with a generous ceiling."""
        # The fake answers at once, so the budget is only reached on a loaded box; a hang test
        # builds its own LocalApi with a short timeout.
        return LocalApi(self.socket_path, timeout=30.0)

    def etag(self) -> str:
        """The ETag the current serve config is served with."""
        return hashlib.sha256(json.dumps(self.serve_config).encode()).hexdigest()

    def answer(self, request: Seen) -> Canned:
        """What this fake says to one request."""
        canned = self.canned.get((request.method, request.path))
        if canned is not None:
            return canned
        if request.path != SERVE_CONFIG_PATH:
            return Canned(status=404, body="no such route")
        if request.method == "GET":
            return Canned(body=json.dumps(self.serve_config), etag=self.etag())
        return self._post(request)

    def _post(self, request: Seen) -> Canned:
        if self.post_status:
            return Canned(status=self.post_status, body="refused")
        if request.headers.get("if-match") != self.etag():
            return Canned(status=412, body="etag mismatch")
        self.serve_config = serve_config(request.body)
        return Canned()


def _handler(fake: FakeTailscaled) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version: str = "HTTP/1.1"

        def _reply(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode() if length else ""
            headers = {key.lower(): value for key, value in self.headers.items()}
            seen = Seen(method, self.path, headers, body)
            fake.requests.append(seen)
            canned = fake.answer(seen)
            if canned.hang:
                _ = fake.released.wait(5.0)
                return
            if canned.late:
                _ = fake.released.wait(canned.late)
            if canned.raw is not None:
                _ = self.wfile.write(canned.raw)
                self.close_connection = True
                return
            self._send(canned)

        def _send(self, canned: Canned) -> None:
            payload = canned.body.encode()
            self.send_response(canned.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            if canned.etag:
                self.send_header("Etag", canned.etag)
            self.end_headers()
            _ = self.wfile.write(payload)

        def do_GET(self) -> None:
            self._reply("GET")

        def do_POST(self) -> None:
            self._reply("POST")

        def address_string(self) -> str:
            return "unix"

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - base signature
            del format, args

    return Handler


@contextmanager
def tailscaled(
    directory: Path,
    *,
    canned: dict[tuple[str, str], Canned] | None = None,
    serve_config: object = None,
    post_status: int = 0,
) -> Generator[FakeTailscaled]:
    """A fake tailscaled listening on a socket under ``directory`` for the block's duration."""
    fake = FakeTailscaled(
        directory / "ts.sock",
        canned=canned or {},
        serve_config=serve_config,
        post_status=post_status,
    )
    server = socketserver.ThreadingUnixStreamServer(str(fake.socket_path), _handler(fake))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield fake
    finally:
        fake.released.set()
        server.shutdown()
        server.server_close()
        thread.join()


def status_route(
    document: object = GOOD_STATUS, *, status: int = 200
) -> dict[tuple[str, str], Canned]:
    """A canned status answer."""
    body = document if isinstance(document, str) else json.dumps(document)
    return {("GET", STATUS_PATH): Canned(status=status, body=body)}


class Clock:
    """A clock tests move by hand; nothing here sleeps."""

    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now: float = now

    def __call__(self) -> float:
        """The current reading."""
        return self.now

    def advance(self, seconds: float) -> None:
        """Move time forward."""
        self.now += seconds


_SESSION = SessionId("5e550001-0000-0000-0000-000000000000")
_PANE = PaneId("w1:p1")


@dataclass(frozen=True)
class Agent:
    """One agent in the shape the engine reports, as far as the notifier reads it."""

    session: SessionId = _SESSION
    status: str = "working"
    target: PaneId = _PANE
    title: str = "fix the parser"
    workspace: str = "herdr"
    project: str = "herdr-pwa"
    state_change_seq: int = 0
