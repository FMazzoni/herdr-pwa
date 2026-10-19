"""The hp-test rig talks to its session over the app's own socket transport, with no herdr CLI."""

import socket
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import JsonValue, ValidationError

from scripts import live_session
from tests.engine_seam import wire_request
from tests.test_transport import FakeHerdr

SESSION = "rig-test"
UUID = "0b5e1c2d-0000-4000-8000-000000000001"


class Runner:
    """Canned answers per method, and every request in arrival order."""

    def __init__(self) -> None:
        self.panes: list[JsonValue] = [{"pane_id": "w1:p1", "agent": "claude"}]
        self.agents: list[list[JsonValue]] = [[]]
        self.seen: list[tuple[JsonValue, JsonValue]] = []
        self.malformed: list[str] = []

    def __call__(self, request: dict[str, JsonValue], _conn: socket.socket) -> dict[str, JsonValue]:
        method, params = request["method"], request.get("params")
        self.seen.append((method, params))
        try:
            # Held to herdr's published schema, since only the reads are tried against a live one.
            wire_request.Request.model_validate(request)
        except ValidationError as exc:
            self.malformed.append(f"{method}: {exc}")
        return {"id": request["id"], "result": self._result(str(method))}

    def _result(self, method: str) -> dict[str, JsonValue]:
        if method == "agent.list":
            agents = self.agents.pop(0) if len(self.agents) > 1 else self.agents[0]
            return {"type": "agent_list", "agents": agents}
        if method == "pane.list":
            return {"type": "pane_list", "panes": self.panes}
        if method == "pane.split":
            return {"type": "pane_info", "pane": {"pane_id": "w1:p2"}}
        if method == "agent.read":
            return {"type": "pane_read", "read": {"text": "Do you trust this folder?"}}
        if method == "agent.start":
            return {"type": "agent_started", "agent": {}, "argv": ["claude", "--model", "haiku"]}
        return {"type": "ok"}

    def methods(self) -> list[JsonValue]:
        return [method for method, _ in self.seen]


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Runner]:
    """A fake runner on the session's socket, and a PATH where no herdr binary can be found."""
    monkeypatch.setenv("PATH", str(tmp_path))
    path = live_session.socket_path(SESSION)
    path.parent.mkdir(parents=True)
    answers = Runner()
    fake = FakeHerdr(path, answers)
    yield answers
    fake.close()
    assert not answers.malformed


def test_status_lists_the_agents_over_the_socket(
    runner: Runner, capsys: pytest.CaptureFixture[str]
) -> None:
    runner.agents = [
        [
            {
                "pane_id": "w1:p1",
                "agent": "claude",
                "name": "probe",
                "agent_status": "idle",
                "agent_session": {"value": UUID},
            }
        ]
    ]
    assert live_session.status(SESSION) == 0
    out = capsys.readouterr().out
    assert f"w1:p1  claude  probe  status=idle  session={UUID}" in out
    assert runner.methods() == ["agent.list", "agent.list"]


def test_a_busy_session_gets_a_pane_split_off_its_first(runner: Runner) -> None:
    assert live_session.free_pane(SESSION, "/work") == "w1:p2"
    assert runner.seen[-1] == (
        "pane.split",
        {"target_pane_id": "w1:p1", "direction": "down", "cwd": "/work"},
    )


def test_the_pane_text_is_the_read_text(runner: Runner) -> None:
    assert live_session.pane_text(SESSION, "w1:p1") == "Do you trust this folder?"
    method, params = runner.seen[-1]
    assert method == "agent.read"
    assert isinstance(params, dict)
    assert (params["target"], params["lines"], params["format"]) == ("w1:p1", 40, "text")


def test_an_agent_starts_with_its_model_and_settles_on_its_uuid(
    runner: Runner, capsys: pytest.CaptureFixture[str]
) -> None:
    runner.panes = [{"pane_id": "w1:p1", "agent": None}]
    ready: list[JsonValue] = [{"pane_id": "w1:p1", "agent_session": {"value": UUID}}]
    runner.agents = [[], ready]
    assert (
        live_session.agent(SESSION, live_session.Launch("probe", "claude", "haiku", "/work")) == 0
    )
    started = [params for method, params in runner.seen if method == "agent.start"]
    assert started == [
        {
            "name": "probe",
            "kind": "claude",
            "pane_id": "w1:p1",
            "args": ["--model", "haiku"],
            "timeout_ms": live_session.START_WAIT_MS,
        }
    ]
    assert f"probe ready on w1:p1, session {UUID}" in capsys.readouterr().out


def test_down_asks_the_server_to_stop(runner: Runner, monkeypatch: pytest.MonkeyPatch) -> None:
    def listening(_path: Path) -> bool:
        # The fake keeps listening, so it counts as stopped once the request went out.
        return "server.stop" not in runner.methods()

    monkeypatch.setattr(live_session, "listening", listening)
    assert live_session.down(SESSION) == 0
    assert runner.methods() == ["server.stop"]
