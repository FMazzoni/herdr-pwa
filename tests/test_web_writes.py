"""Every write route over a fake herd: what it sends, what it refuses, and at which status."""

from dataclasses import dataclass
from pathlib import Path

from fastapi.testclient import TestClient

from herdr_pwa.audit import records
from herdr_pwa.engine.client import HerdrClient
from herdr_pwa.engine.config import EngineSettings
from herdr_pwa.engine.dialog import BlockedPrompt
from herdr_pwa.engine.model import screen_digest
from herdr_pwa.settings import Settings
from herdr_pwa.web.app import create_app
from herdr_pwa.writes.outcome import Pacing
from tests._fake_herd import P3, S1, S2, SETTLED, FakeClock, FakeHerd, capture
from tests.webfakes import IDENTITY

ORIGIN = {"Origin": "http://testserver"}
PERMISSION = capture("capture-blocked-permission-prompt")
FINGERPRINT = BlockedPrompt.parse(PERMISSION).fingerprint


@dataclass
class Web:
    herd: FakeHerd
    http: TestClient
    state: Path

    def post(self, path: str, **data: str) -> object:
        return self.http.post(path, data=data, headers=ORIGIN, follow_redirects=False)

    @property
    def audit(self) -> list[dict[str, object]]:
        return records(self.state, limit=0)


def web(tmp_path: Path, herd: FakeHerd) -> Web:
    clock = FakeClock()
    settings = Settings.model_validate(
        {
            "socket_path": tmp_path / "no-runner.sock",
            "state_dir": tmp_path / "state",
            "tailscaled_socket": tmp_path / "no-tailscaled.sock",
            "snapshot_cache_ttl": 0.001,
        }
    )

    def factory(engine: EngineSettings) -> HerdrClient:
        return HerdrClient(engine, transport=herd, clock=clock, sleep=clock.sleep)

    app = create_app(settings, factory, clock=clock, pacing=Pacing(sleep=clock.sleep, clock=clock))
    return Web(herd, TestClient(app, headers=IDENTITY), tmp_path / "state")


def idle(tmp_path: Path, *screens: str) -> Web:
    herd = FakeHerd()
    herd.put(P3, S1)
    herd.screens[P3] = list(screens) or [SETTLED]
    return web(tmp_path, herd)


def blocked(tmp_path: Path) -> Web:
    herd = FakeHerd()
    herd.put(P3, S1, status="blocked")
    herd.screens[P3] = [PERMISSION]
    return web(tmp_path, herd)


def test_a_write_without_a_same_origin_header_is_refused(tmp_path: Path) -> None:
    w = idle(tmp_path)
    response = w.http.post(f"/agent/{S1}/prompt", data={"text": "hi"})
    assert response.status_code == 403
    assert w.herd.writes("agent.prompt") == []


def test_the_composer_carries_the_idle_screen_digest_and_a_draft_key(tmp_path: Path) -> None:
    w = idle(tmp_path)
    response = w.http.get(f"/agent/{S1}/compose")
    assert response.status_code == 200
    assert screen_digest(SETTLED) in response.text
    assert f'data-hp-draft="{S1}"' in response.text


def test_a_prompt_is_sent_audited_and_read_back(tmp_path: Path) -> None:
    w = idle(tmp_path, SETTLED, SETTLED + "  hello there\n")
    response = w.http.post(f"/agent/{S1}/prompt", data={"text": "hello there"}, headers=ORIGIN)
    assert response.status_code == 200
    assert "Sent, and it is on the pane." in response.text
    assert "data-hp-sent" in response.text
    assert w.herd.writes("agent.prompt") == [{"target": "w1:p3", "text": "hello there"}]
    attempt = w.audit[0]
    assert (attempt["endpoint"], attempt["actor"]) == ("prompt", "operator@example.com")


def test_a_prompt_at_a_question_is_409_and_keeps_the_text(tmp_path: Path) -> None:
    w = idle(tmp_path, PERMISSION)
    response = w.http.post(f"/agent/{S1}/prompt", data={"text": "keep me"}, headers=ORIGIN)
    assert response.status_code == 409
    assert "showing a question" in response.text
    assert "keep me" in response.text
    assert response.headers["hp-fragment"] == "1"
    assert w.herd.writes("agent.prompt") == []


def test_a_destructive_prompt_is_held_with_its_reason_then_sent(tmp_path: Path) -> None:
    w = idle(tmp_path)
    held = w.http.post(f"/agent/{S1}/prompt", data={"text": "rm -rf build"}, headers=ORIGIN)
    assert held.status_code == 200
    assert "rm with a recursive flag" in held.text
    assert 'name="confirm" value="1"' in held.text
    assert w.herd.writes("agent.prompt") == []
    data = {"text": "rm -rf build", "confirm": "1", "screen": screen_digest(SETTLED)}
    _ = w.http.post(f"/agent/{S1}/prompt", data=data, headers=ORIGIN)
    assert len(w.herd.writes("agent.prompt")) == 1


def test_a_prompt_to_a_gone_agent_is_404(tmp_path: Path) -> None:
    w = idle(tmp_path)
    response = w.http.post(f"/agent/{S2}/prompt", data={"text": "hi"}, headers=ORIGIN)
    assert response.status_code == 404


def test_a_pane_address_in_the_session_slot_is_a_bad_request(tmp_path: Path) -> None:
    w = idle(tmp_path)
    response = w.http.post("/agent/w1:p3/prompt", data={"text": "hi"}, headers=ORIGIN)
    assert response.status_code == 400
    assert w.herd.writes("agent.prompt") == []


def test_the_answer_region_draws_only_keys_the_write_will_take(tmp_path: Path) -> None:
    w = blocked(tmp_path)
    response = w.http.get(f"/agent/{S1}/answer")
    assert response.status_code == 200
    for digit in ("1", "2", "3"):
        assert f'name="key" value="{digit}"' in response.text
    assert FINGERPRINT in response.text
    assert "Do you want to proceed?" in response.text


def test_a_tapped_digit_answers_and_the_controls_go_with_the_question(tmp_path: Path) -> None:
    w = blocked(tmp_path)

    def answered(herd: FakeHerd, _params: object) -> None:
        herd.agents[P3]["agent_status"] = "working"

    w.herd.after["agent.send_keys"] = answered
    response = w.http.post(
        f"/agent/{S1}/keys", data={"key": "1", "screen": FINGERPRINT}, headers=ORIGIN
    )
    assert response.status_code == 200
    assert "confirmed" in response.text
    assert 'value="1"' not in response.text, "no live digit under a question that went away"
    assert w.herd.writes("agent.send_keys") == [{"target": "w1:p3", "keys": ["1"]}]


def test_a_key_the_screen_does_not_offer_is_400(tmp_path: Path) -> None:
    w = blocked(tmp_path)
    response = w.http.post(
        f"/agent/{S1}/keys", data={"key": "9", "screen": FINGERPRINT}, headers=ORIGIN
    )
    assert response.status_code == 400
    assert w.herd.writes("agent.send_keys") == []


def test_a_stale_control_is_409_and_redrawn_from_the_screen_now(tmp_path: Path) -> None:
    w = blocked(tmp_path)
    response = w.http.post(f"/agent/{S1}/keys", data={"key": "1", "screen": "old"}, headers=ORIGIN)
    assert response.status_code == 409
    assert FINGERPRINT in response.text
    assert w.herd.writes("agent.send_keys") == []


def test_interrupt_asks_twice(tmp_path: Path) -> None:
    w = blocked(tmp_path)
    first = w.http.post(f"/agent/{S1}/keys", data={"key": "ctrl-c"}, headers=ORIGIN)
    assert "Really interrupt" in first.text
    assert w.herd.writes("agent.send_keys") == []
    _ = w.http.post(f"/agent/{S1}/keys", data={"key": "ctrl-c", "confirm": "1"}, headers=ORIGIN)
    assert len(w.herd.writes("agent.send_keys")) == 1


def test_ending_an_agent_needs_the_confirm_the_route_enforces(tmp_path: Path) -> None:
    herd = FakeHerd()
    herd.put(P3, S1, status="working")
    w = web(tmp_path, herd)
    bare = w.http.post(f"/agent/{S1}/close", data={}, headers=ORIGIN, follow_redirects=False)
    assert bare.status_code == 200
    assert herd.writes("pane.close") == []
    unacked = w.http.post(
        f"/agent/{S1}/close", data={"confirm": "1"}, headers=ORIGIN, follow_redirects=False
    )
    assert "working" in unacked.text
    assert herd.writes("pane.close") == []
    done = w.http.post(
        f"/agent/{S1}/close",
        data={"confirm": "1", "acknowledge": "1"},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert done.status_code == 303
    assert herd.writes("pane.close") == [{"pane_id": "w1:p3"}]
    assert w.audit[0]["endpoint"] == "close"


def test_the_new_agent_form_offers_the_three_harnesses_on_this_machine(tmp_path: Path) -> None:
    herd = FakeHerd()
    herd.add_workspace("w1")
    w = web(tmp_path, herd)
    response = w.http.get("/new")
    assert response.status_code == 200
    assert "bash set to ask" in response.text


def test_starting_an_agent_redirects_to_its_page_or_its_door(tmp_path: Path) -> None:
    herd = FakeHerd()
    herd.add_workspace("w1")
    herd.started_session = S1
    w = web(tmp_path, herd)
    data = {"name": "probe", "kind": "claude", "workspace": "w1", "cwd": str(tmp_path)}
    response = w.http.post("/new", data=data, headers=ORIGIN, follow_redirects=False)
    if response.status_code == 400:
        assert "kind is not one" in response.text  # claude not installed on this box
        return
    assert response.status_code == 303
    assert response.headers["location"] == f"/agent/{S1}"


def test_an_unusable_start_form_is_400_and_starts_nothing(tmp_path: Path) -> None:
    herd = FakeHerd()
    herd.add_workspace("w1")
    w = web(tmp_path, herd)
    data = {"name": "!!", "kind": "claude", "workspace": "w1", "cwd": "/"}
    response = w.http.post("/new", data=data, headers=ORIGIN)
    assert response.status_code == 400
    assert herd.writes("tab.create") == []


def test_the_starting_door_redirects_once_there_is_a_session(tmp_path: Path) -> None:
    w = idle(tmp_path)
    response = w.http.get("/starting/w1:p3", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/agent/{S1}"


def test_the_starting_door_sends_a_pad_key_to_a_session_less_agent(tmp_path: Path) -> None:
    herd = FakeHerd()
    herd.put(P3, "", status="blocked", launch_pending=True)
    w = web(tmp_path, herd)
    page = w.http.get("/starting/w1:p3")
    assert page.status_code == 200
    response = w.http.post("/starting/w1:p3/keys", data={"key": "enter"}, headers=ORIGIN)
    assert response.status_code == 200
    assert herd.writes("agent.send_keys") == [{"target": "w1:p3", "keys": ["enter"]}]


def test_workspace_new_rename_and_close(tmp_path: Path) -> None:
    herd = FakeHerd()
    herd.put(P3, S1)
    w = web(tmp_path, herd)
    made = w.http.post(
        "/workspaces/new", data={"label": "scratch"}, headers=ORIGIN, follow_redirects=False
    )
    assert made.status_code == 303
    renamed = w.http.post(
        "/workspaces/w7/rename", data={"label": "renamed"}, headers=ORIGIN, follow_redirects=False
    )
    assert renamed.status_code == 303
    assert herd.workspaces["w7"]["label"] == "renamed"
    bad = w.http.post("/workspaces/w7/rename", data={"label": ""}, headers=ORIGIN)
    assert bad.status_code == 400
    first = w.http.post("/workspaces/w1/close", data={"confirm": "1"}, headers=ORIGIN)
    assert first.status_code == 200
    assert 'name="saw" value="1"' in first.text
    assert herd.writes("workspace.close") == []
    closed = w.http.post(
        "/workspaces/w1/close",
        data={"confirm": "1", "armed": "1", "saw": "1"},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert closed.status_code == 303
    assert herd.writes("workspace.close") == [{"workspace_id": "w1"}]


def test_a_missing_workspace_is_404(tmp_path: Path) -> None:
    w = idle(tmp_path)
    assert w.http.get("/workspaces/w404/close").status_code == 404
    assert w.http.get("/workspaces/w404/rename").status_code == 404
