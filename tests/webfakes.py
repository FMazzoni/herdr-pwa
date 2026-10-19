"""A herd on a fake wire, and a web app built around it that touches no real runner."""

import copy
import json
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import JsonValue, TypeAdapter
from starlette.routing import BaseRoute

from herdr_pwa.engine import EngineSettings, HerdrClient, WireReply
from herdr_pwa.settings import Settings
from herdr_pwa.web.app import create_app
from herdr_pwa.web.assets import SCRIPTS, STATIC_DIR
from herdr_pwa.web.seams import Seams
from herdr_pwa.writes.outcome import Pacing
from tests._jsonpath import at, obj
from tests._wire_support import as_reply
from tests.engine_seam import method_of, wire_request

FIXTURES = Path(__file__).parent / "fixtures"
SNAPSHOT_FIXTURE = FIXTURES / "snapshot.example.json"
LOGIN = "operator@example.com"
IDENTITY = {"Tailscale-User-Login": LOGIN}


def client_scripts() -> str:
    """Every script of this app's own that a page loads, in load order, as one text."""
    return "\n".join((STATIC_DIR / name).read_text(encoding="utf-8") for name in SCRIPTS)


SNAPSHOT = method_of(wire_request.SessionSnapshotRequest)
READ = method_of(wire_request.AgentReadRequest)
AGENT_GET = method_of(wire_request.AgentGetRequest)
SEND_KEYS = method_of(wire_request.AgentSendKeysRequest)
PROMPT = method_of(wire_request.AgentPromptRequest)

# The fixture's working agent, and the first idle one.
WORKING_SESSION = "6ae1e031-ddb4-4684-ab07-68073f7fec47"
WORKING_PANE = "w1:p3"
IDLE_SESSION = "5b609dba-d756-493a-a3fc-b4e09bf0a183"

# A callable answers from the request, as a runner answers a text and a colour read differently.
type Reply = (
    dict[str, JsonValue] | Exception | Callable[[dict[str, JsonValue]], dict[str, JsonValue]]
)


def snapshot_frame() -> dict[str, JsonValue]:
    """A fresh, mutable copy of the recorded snapshot frame."""
    return TypeAdapter(dict[str, JsonValue]).validate_json(SNAPSHOT_FIXTURE.read_text())


def agents_of(frame: dict[str, JsonValue]) -> list[dict[str, JsonValue]]:
    """The raw agent objects inside a snapshot frame, for a test to edit in place."""
    agents = at(frame, "result", "snapshot", "agents")
    assert isinstance(agents, list)
    return [obj(a) for a in agents]


def read_frame(text: str, pane: str = WORKING_PANE) -> dict[str, JsonValue]:
    """An ``agent.read`` reply carrying ``text``, read from ``pane``."""
    workspace = pane.split(":", 1)[0]
    return {
        "id": "x",
        "result": {
            "type": "pane_read",
            "read": {
                "format": "ansi",
                "pane_id": pane,
                "revision": 7,
                "source": "visible",
                "tab_id": f"{workspace}:t1",
                "text": text,
                "truncated": False,
                "workspace_id": workspace,
            },
        },
    }


@dataclass
class FakeWire:
    """Answers each socket method with a canned frame or raises the canned failure.

    A key of ``"<method> <target>"`` answers that method for one pane only, ahead of the
    plain ``"<method>"`` key that answers it for every other pane.
    """

    replies: dict[str, Reply] = field(default_factory=dict[str, Reply])
    calls: list[str] = field(default_factory=list[str])
    # Run once a method has answered, to model what the terminal did with a write.
    after: dict[str, Callable[["FakeWire"], None]] = field(
        default_factory=dict[str, Callable[["FakeWire"], None]]
    )

    def call(
        self,
        method: str,
        params: dict[str, JsonValue],
        *,
        timeout: float | None = None,
        idempotent: bool = True,
    ) -> WireReply:
        del timeout, idempotent
        self.calls.append(method)
        target = params.get("target")
        reply = self.replies.get(f"{method} {target}") if isinstance(target, str) else None
        reply = reply if reply is not None else self.replies.get(method)
        if reply is None:
            raise AssertionError(f"the test gave no reply for {method}")
        if isinstance(reply, Exception):
            raise reply
        hook = self.after.get(method)
        if hook is not None:
            hook(self)
        return as_reply(reply(params) if callable(reply) else copy.deepcopy(reply))

    def close(self) -> None:
        return None


# How long a held write waits for its release before answering anyway, so no server thread
# outlives the browser run that left it held.
HOLD_SECONDS = 60.0


@dataclass(frozen=True)
class Hold:
    """An ``after`` hook that sits on a write until ``release`` is set, then runs ``then``.

    The write's request stays open meanwhile, so the page can be seen while its tap is out.
    """

    then: Callable[[FakeWire], None] | None = None
    ceiling: float = HOLD_SECONDS
    release: threading.Event = field(default_factory=threading.Event)
    # Set once the write has reached the runner and is being held.
    taken: threading.Event = field(default_factory=threading.Event)
    # Set once the write was let go by ``release``, not by the ceiling.
    released: threading.Event = field(default_factory=threading.Event)

    def __call__(self, wire: FakeWire) -> None:
        self.taken.set()
        if self.release.wait(self.ceiling):
            self.released.set()
        if self.then is not None:
            self.then(wire)


@dataclass
class Clock:
    """A clock the test moves by hand."""

    now: float = 1000.0
    # How far each read moves it; nonzero is a clock that runs by itself, one tick per look.
    step: float = 0.0

    def __call__(self) -> float:
        self.now += self.step
        return self.now

    def sleep(self, seconds: float) -> None:
        """Wait by moving the clock, so a watched write settles without real time passing."""
        self.now += seconds


@dataclass
class Rig:
    """One app, its fake wire, its clock and a client that carries an identity."""

    wire: FakeWire
    clock: Clock
    http: TestClient
    settings: Settings


def build_rig(
    tmp_path: Path,
    replies: Mapping[str, Reply] | None = None,
    *,
    headers: dict[str, str] | None = None,
    paced: bool = False,
    **overrides: object,
) -> Rig:
    """An app whose every destination is under ``tmp_path`` and whose runner is fake."""
    given: dict[str, Reply] = dict(replies) if replies is not None else {SNAPSHOT: snapshot_frame()}
    # A stopped agent's screen is read for a dialog herdr missed; unless a test says otherwise,
    # every pane shows an empty prompt, as a real runner would answer.
    _ = given.setdefault(READ, read_frame("> "))
    wire = FakeWire(given)
    clock = Clock()
    fields: dict[str, object] = {
        "socket_path": tmp_path / "no-runner.sock",
        "state_dir": tmp_path / "state",
        "transcript_roots": (str(tmp_path / "transcripts"),),
        "tailscaled_socket": tmp_path / "no-tailscaled.sock",
        # The background loop is driven by hand in its own tests, never by a request test.
        "push_notifier": False,
        **overrides,
    }
    settings = Settings.model_validate(fields)

    def factory(engine: EngineSettings) -> HerdrClient:
        return HerdrClient(engine, transport=wire, clock=clock)

    # Paced, a write's watch waits on the rig's clock instead of the wall's.
    pacing = Pacing(sleep=clock.sleep, clock=clock) if paced else Pacing()
    app = create_app(settings, Seams(clock=clock, pacing=pacing, client_factory=factory))
    http = TestClient(app, headers=IDENTITY if headers is None else headers)
    return Rig(wire, clock, http, settings)


def _routes_in(routes: Sequence[BaseRoute]) -> list[APIRoute]:
    found: list[APIRoute] = []
    for route in routes:
        if isinstance(route, APIRoute):
            found.append(route)
        # An included router keeps its own table behind it.
        inner = getattr(route, "original_router", None)
        if isinstance(inner, APIRouter):
            found += _routes_in(inner.routes)
    return found


def routes_of(rig: Rig) -> list[APIRoute]:
    """Every route the rig's app answers, those of included routers too."""
    app = rig.http.app
    assert isinstance(app, FastAPI)
    return _routes_in(app.routes)


def write_transcript(root: Path, session: str, records: list[dict[str, JsonValue]]) -> None:
    """Lay a Claude Code transcript out the way Claude Code does, or replace one whole.

    Claude Code only appends; a walk that rewrites the log swaps a new file in, since writing
    it in place leaves it empty for a moment, and a page told of that moment draws no chat."""
    project = root / "-tmp"
    project.mkdir(parents=True, exist_ok=True)
    lines = (json.dumps(r) for r in records)
    log = project / f"{session}.jsonl"
    fresh = log.with_name(log.name + ".new")
    fresh.write_text("\n".join(lines) + "\n")
    fresh.replace(log)


def assistant_says(text: str, uuid: str = "a1") -> dict[str, JsonValue]:
    """One assistant prose record."""
    return {
        "type": "assistant",
        "uuid": uuid,
        "timestamp": "2026-10-10T10:00:00.000Z",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def human_says(text: str, uuid: str = "u1") -> dict[str, JsonValue]:
    """One typed human record."""
    return {
        "type": "user",
        "uuid": uuid,
        "timestamp": "2026-10-10T09:59:00.000Z",
        "promptSource": "typed",
        "origin": {"kind": "human"},
        "message": {"role": "user", "content": text},
    }
