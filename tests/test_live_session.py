"""The hp-test rig talks to its session over the app's own socket transport, with no herdr CLI."""

import socket
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import JsonValue, ValidationError

from herdr_pwa.engine import Frame
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
        self.read = "Do you trust this folder?"
        self.busy = 0  # how many agent.start calls find the new shell still starting

    def __call__(self, request: dict[str, JsonValue], _conn: socket.socket) -> dict[str, JsonValue]:
        method, params = request["method"], request.get("params")
        self.seen.append((method, params))
        if method == "agent.start" and self.busy:
            self.busy -= 1
            busy: JsonValue = {
                "code": "agent_pane_busy",
                "message": "pane is not an available shell",
            }
            return {"id": request["id"], "error": busy}
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
            return {"type": "pane_read", "read": {"text": self.read}}
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


def test_the_rig_strips_every_session_marker_and_keeps_the_rest() -> None:
    ambient = {
        "HERDR_PANE_ID": "w1:p1",
        "HERDR_SESSION": "default",
        "HERDR_PWA_MANAGED": "1",
        "CLAUDECODE": "1",
        "CLAUDE_CODE_CHILD_SESSION": "1",
        "CLAUDE_CODE_SESSION_ID": UUID,
        "CLAUDE_PID": "42",
        "CLAUDE_EFFORT": "high",
        "AI_AGENT": "claude-code",
        "HOME": "/home/user",
        "PATH": "/usr/bin",
        "CLAUDE_CONFIG_DIR": "/home/user/.claude",
    }
    assert live_session.clean_env(ambient) == {
        "HOME": "/home/user",
        "PATH": "/usr/bin",
        "CLAUDE_CONFIG_DIR": "/home/user/.claude",
    }


def test_a_new_pane_is_split_off_the_first_in_the_given_folder() -> None:
    panes: list[Frame] = [{"pane_id": "w1:p1", "agent": None}, {"pane_id": "w1:p3"}]
    assert live_session.split_request(panes, "/work") == {
        "target_pane_id": "w1:p1",
        "direction": "down",
        "cwd": "/work",
    }


def test_a_session_without_panes_has_nothing_to_split() -> None:
    with pytest.raises(SystemExit):
        _ = live_session.split_request([], "/work")


# Claude Code's folder-trust menu as a pane shows it, its cursor on the option that quits.
TRUST_MENU = """ Accessing workspace:

 Claude Code'll be able to read, edit, and execute files here.

 \u276f No, exit
   Yes, I trust this folder

 Enter to confirm \u00b7 Esc to cancel"""


def test_the_trust_menu_is_answered_by_moving_to_yes_before_confirming() -> None:
    assert live_session.trust_keys(TRUST_MENU) == ["Down", "Enter"]
    on_yes = TRUST_MENU.replace("\u276f No", "  No").replace("  Yes", "\u276f Yes")
    assert live_session.trust_keys(on_yes) == ["Enter"]


def test_a_pane_without_the_trust_menu_gets_no_keys() -> None:
    assert live_session.trust_keys("\u276f Try something") is None
    assert live_session.trust_keys(TRUST_MENU.replace("Yes, I trust this folder", "")) is None


def test_the_pane_text_is_the_read_text(runner: Runner) -> None:
    assert live_session.pane_text(SESSION, "w1:p1") == "Do you trust this folder?"
    method, params = runner.seen[-1]
    assert method == "agent.read"
    assert isinstance(params, dict)
    assert (params["target"], params["lines"], params["format"]) == ("w1:p1", 40, "text")


def test_an_agent_starts_with_its_model_and_settles_on_its_uuid(
    runner: Runner, capsys: pytest.CaptureFixture[str]
) -> None:
    # An idle pane is no use: it sits in whatever folder it was left in.
    runner.panes = [{"pane_id": "w1:p1", "agent": None}]
    ready: list[JsonValue] = [{"pane_id": "w1:p2", "agent_session": {"value": UUID}}]
    runner.agents = [[], ready]
    assert (
        live_session.agent(SESSION, live_session.Launch("probe", "claude", "haiku", "/work")) == 0
    )
    split = [params for method, params in runner.seen if method == "pane.split"]
    assert split == [{"target_pane_id": "w1:p1", "direction": "down", "cwd": "/work"}]
    started = [params for method, params in runner.seen if method == "agent.start"]
    assert started == [
        {
            "name": "probe",
            "kind": "claude",
            "pane_id": "w1:p2",
            "args": ["--model", "haiku"],
            "timeout_ms": live_session.START_WAIT_MS,
        }
    ]
    assert f"probe ready on w1:p2, session {UUID}" in capsys.readouterr().out


def test_a_start_waits_out_a_shell_that_is_still_starting(
    runner: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(live_session, "SETTLE_STEP", 0.01)
    runner.busy = 2
    launch: Frame = {"name": "probe", "kind": "claude", "pane_id": "w1:p2"}
    assert live_session.start(SESSION, launch)["argv"] == ["claude", "--model", "haiku"]
    assert runner.methods().count("agent.start") == 3


def test_the_trust_prompt_gets_its_yes_key_and_never_a_bare_enter(
    runner: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(live_session, "SETTLE_STEP", 0.01)
    runner.agents = [[], [], [{"pane_id": "w1:p2", "agent_session": {"value": UUID}}]]
    runner.read = TRUST_MENU
    assert live_session.settle(SESSION, "w1:p2") == UUID
    keys = [params for method, params in runner.seen if method == "agent.send_keys"]
    assert keys == [{"target": "w1:p2", "keys": ["Down", "Enter"]}]


def test_down_asks_the_server_to_stop(runner: Runner, monkeypatch: pytest.MonkeyPatch) -> None:
    def listening(_path: Path) -> bool:
        # The fake keeps listening, so it counts as stopped once the request went out.
        return "server.stop" not in runner.methods()

    monkeypatch.setattr(live_session, "listening", listening)
    assert live_session.down(SESSION) == 0
    assert runner.methods() == ["server.stop"]
