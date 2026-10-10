"""A herd on a fake wire, and a web app built around it that touches no real runner."""

import copy
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import JsonValue

from herdr_pwa.engine import client as engine_client
from herdr_pwa.engine.client import HerdrClient
from herdr_pwa.engine.config import EngineSettings
from herdr_pwa.engine.generated import request as wire_request
from herdr_pwa.jsontypes import JsonObject, as_object, loads
from herdr_pwa.settings import Settings
from herdr_pwa.web.app import create_app
from herdr_pwa.writes.outcome import Pacing

FIXTURES = Path(__file__).parent / "fixtures"
SNAPSHOT_FIXTURE = FIXTURES / "snapshot.example.json"
LOGIN = "operator@example.com"
IDENTITY = {"Tailscale-User-Login": LOGIN}

SNAPSHOT = engine_client.method_of(wire_request.SessionSnapshotRequest)
READ = engine_client.method_of(wire_request.AgentReadRequest)
AGENT_GET = engine_client.method_of(wire_request.AgentGetRequest)
SEND_KEYS = engine_client.method_of(wire_request.AgentSendKeysRequest)

# The fixture's working agent, and the first idle one.
WORKING_SESSION = "6ae1e031-ddb4-4684-ab07-68073f7fec47"
WORKING_PANE = "w1:p3"
IDLE_SESSION = "5b609dba-d756-493a-a3fc-b4e09bf0a183"

type Reply = JsonObject | Exception


def _object(value: JsonValue) -> JsonObject:
    found = as_object(value)
    assert found is not None
    return found


def snapshot_frame() -> JsonObject:
    """A fresh, mutable copy of the recorded snapshot frame."""
    return _object(loads(SNAPSHOT_FIXTURE.read_text()))


def agents_of(frame: JsonObject) -> list[JsonObject]:
    """The raw agent objects inside a snapshot frame, for a test to edit in place."""
    snapshot = _object(_object(frame["result"])["snapshot"])
    agents = snapshot["agents"]
    assert isinstance(agents, list)
    return [_object(a) for a in agents]


def read_frame(text: str, pane: str = WORKING_PANE) -> JsonObject:
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
        params: JsonObject,
        *,
        timeout: float | None = None,
        idempotent: bool = True,
    ) -> JsonObject:
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
        return copy.deepcopy(reply)

    def close(self) -> None:
        return None


@dataclass
class Clock:
    """A clock the test moves by hand."""

    now: float = 1000.0

    def __call__(self) -> float:
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
    wire = FakeWire(dict(replies) if replies is not None else {SNAPSHOT: snapshot_frame()})
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
    pacing = Pacing(sleep=clock.sleep, clock=clock) if paced else None
    app = create_app(settings, factory, clock=clock, pacing=pacing)
    http = TestClient(app, headers=IDENTITY if headers is None else headers)
    return Rig(wire, clock, http, settings)


def write_transcript(root: Path, session: str, records: list[JsonObject]) -> None:
    """Lay a Claude Code transcript out the way Claude Code does."""
    project = root / "-tmp"
    project.mkdir(parents=True, exist_ok=True)
    lines = (json.dumps(r) for r in records)
    (project / f"{session}.jsonl").write_text("\n".join(lines) + "\n")


def assistant_says(text: str, uuid: str = "a1") -> JsonObject:
    """One assistant prose record."""
    return {
        "type": "assistant",
        "uuid": uuid,
        "timestamp": "2026-10-10T10:00:00.000Z",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def human_says(text: str, uuid: str = "u1") -> JsonObject:
    """One typed human record."""
    return {
        "type": "user",
        "uuid": uuid,
        "timestamp": "2026-10-10T09:59:00.000Z",
        "promptSource": "typed",
        "origin": {"kind": "human"},
        "message": {"role": "user", "content": text},
    }
