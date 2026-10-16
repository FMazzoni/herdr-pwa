"""Every write route over a fake herd: what it sends, what it refuses, and at which status."""

import re
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import JsonValue, TypeAdapter

from herdr_pwa.audit import Record, records
from herdr_pwa.engine import (
    BlockedPrompt,
    EngineSettings,
    EngineUnavailable,
    HerdrClient,
    screen_digest,
)
from herdr_pwa.settings import Settings
from herdr_pwa.web.app import create_app
from herdr_pwa.writes.keys import control_token
from herdr_pwa.writes.outcome import Pacing
from tests._fake_herd import P3, P5, S1, S2, SETTLED, FakeClock, FakeHerd, capture, echoed
from tests.webfakes import IDENTITY

ORIGIN = {"Origin": "http://testserver"}
PERMISSION = capture("capture-blocked-permission-prompt")
FINGERPRINT = control_token(BlockedPrompt.parse(PERMISSION))


@dataclass
class Web:
    herd: FakeHerd
    http: TestClient
    state: Path

    def post(self, path: str, **data: str) -> object:
        return self.http.post(path, data=data, headers=ORIGIN, follow_redirects=False)

    @property
    def audit(self) -> list[Record]:
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
    w = idle(tmp_path, SETTLED, echoed("\u276f hello there"))
    response = w.http.post(f"/agent/{S1}/prompt", data={"text": "hello there"}, headers=ORIGIN)
    assert response.status_code == 200
    assert "Sent, and it is on the pane." in response.text
    assert "data-hp-sent" in response.text
    assert w.herd.writes("agent.prompt") == [{"target": "w1:p3", "text": "hello there"}]
    attempt = w.audit[0]
    assert attempt["phase"] == "attempt"
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


def _lost_after_send(_herd: FakeHerd, _params: dict[str, JsonValue]) -> None:
    lost = EngineUnavailable("the runner closed the connection while answering")
    lost.after_send = True
    raise lost


def _posted_back(page: str) -> dict[str, str]:
    """What the rendered composer would post: its hidden fields and its textarea."""
    fields = dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)"', page))
    text = re.search(r"<textarea[^>]*>([^<]*)</textarea>", page)
    assert text is not None
    return {**fields, "text": text.group(1)}


def test_a_prompt_that_may_have_landed_is_not_sent_again_without_saying_so(
    tmp_path: Path,
) -> None:
    w = idle(tmp_path)
    w.herd.after["agent.prompt"] = _lost_after_send
    lost = w.http.post(f"/agent/{S1}/prompt", data={"text": "hello there"}, headers=ORIGIN)
    assert lost.status_code == 502
    assert "may or may not have been sent" in lost.text
    assert "Send again anyway" in lost.text
    form_back = _posted_back(lost.text)
    assert form_back["screen"] == screen_digest(SETTLED), "a fresh digest, not an empty one"
    del w.herd.after["agent.prompt"]
    # The plain resubmit, as a stray tap or a restored draft would send it.
    plain = {k: v for k, v in form_back.items() if k != "again"}
    held = w.http.post(f"/agent/{S1}/prompt", data=plain, headers=ORIGIN)
    assert held.status_code == 200
    assert "may already have landed" in held.text
    assert "Send again anyway" in held.text
    assert len(w.herd.writes("agent.prompt")) == 1
    again = w.http.post(f"/agent/{S1}/prompt", data=_posted_back(held.text), headers=ORIGIN)
    assert again.status_code == 200
    assert len(w.herd.writes("agent.prompt")) == 2
    # Once something was sent, the next prompt needs no extra word.
    _ = w.http.post(f"/agent/{S1}/prompt", data={"text": "hello there"}, headers=ORIGIN)
    assert len(w.herd.writes("agent.prompt")) == 3


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
    attempt = w.audit[0]
    assert attempt["phase"] == "attempt"
    assert attempt["endpoint"] == "close"


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
    saw = re.search(r'name="saw" value="([0-9a-f]+)"', first.text)
    assert saw is not None
    assert herd.writes("workspace.close") == []
    stale = w.http.post(
        "/workspaces/w1/close", data={"confirm": "1", "armed": "1", "saw": "1"}, headers=ORIGIN
    )
    assert stale.status_code == 409
    assert herd.writes("workspace.close") == []
    closed = w.http.post(
        "/workspaces/w1/close",
        data={"confirm": "1", "armed": "1", "saw": saw.group(1)},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert closed.status_code == 303
    assert herd.writes("workspace.close") == [{"workspace_id": "w1"}]


def test_a_missing_workspace_is_404(tmp_path: Path) -> None:
    w = idle(tmp_path)
    assert w.http.get("/workspaces/w404/close").status_code == 404
    assert w.http.get("/workspaces/w404/rename").status_code == 404


def test_a_sheet_write_under_htmx_replaces_rather_than_redirects(tmp_path: Path) -> None:
    herd = FakeHerd()
    herd.put(P3, S1)
    w = web(tmp_path, herd)
    made = w.http.post(
        "/workspaces/new",
        data={"label": "scratch"},
        headers={**ORIGIN, "HX-Request": "true"},
        follow_redirects=False,
    )
    assert made.status_code == 200
    assert "location" not in made.headers
    assert "HX-Redirect" not in made.headers
    assert made.headers["hx-reswap"] == "none"
    trigger = TypeAdapter(JsonValue).validate_json(made.headers["hx-trigger"])
    assert trigger == {"hp:opened": {"where": "/workspaces#w7"}}


def test_every_form_that_navigates_is_a_sheet(tmp_path: Path) -> None:
    herd = FakeHerd()
    herd.put(P3, S1)
    herd.put(P5, "", status="blocked", launch_pending=True)
    w = web(tmp_path, herd)
    for path in (
        f"/agent/{S1}/close",
        "/new",
        "/workspaces/new",
        "/workspaces/w1/rename",
        "/workspaces/w1/close",
        "/starting/w1:p5",
    ):
        body = w.http.get(path).text
        assert body.count("<form") == body.count("data-hp-sheet") > 0, path


def _blocked_on(tmp_path: Path, screen: str) -> Web:
    herd = FakeHerd()
    herd.put(P3, S1, status="blocked")
    herd.screens[P3] = [screen]
    return web(tmp_path, herd)


def test_an_option_row_is_a_numbered_tap_target_that_guards_the_whole_region(
    tmp_path: Path,
) -> None:
    text = blocked(tmp_path).http.get(f"/agent/{S1}/answer").text
    assert '<span class="option__n" aria-hidden="true">1</span>' in text
    # One tap disables every control in the region, and outranks a poll already in flight.
    assert 'hx-disabled-elt="#answer button"' in text
    assert 'hx-sync="closest .answer:replace"' in text
    assert "option--still" not in text


def test_a_checklist_says_why_its_rows_are_not_buttons_and_links_the_pane(
    tmp_path: Path,
) -> None:
    w = _blocked_on(tmp_path, capture("capture-blocked-multiselect-checklist"))
    text = w.http.get(f"/agent/{S1}/answer").text
    assert "option--still" in text
    assert "option__box--on" in text, "the ticked box is drawn ticked"
    assert "Submit is the last row" in text
    assert "Tick / Enter" in text
    assert "What this screen says" not in text, "its hints only repeat the keys under the rows"
    assert f'href="/agent/{S1}?tab=pane#agent-view"' in text
    assert 'name="key" value="1"' not in text


def test_a_question_that_cannot_be_read_says_so_with_a_way_to_the_pane(tmp_path: Path) -> None:
    w = _blocked_on(tmp_path, "")
    text = w.http.get(f"/agent/{S1}/answer").text
    assert "its question could not be read here" in text
    assert f'href="/agent/{S1}?tab=pane#agent-view"' in text


def test_a_key_that_left_the_question_up_says_so_and_does_not_claim_an_answer(
    tmp_path: Path,
) -> None:
    w = blocked(tmp_path)
    response = w.http.post(
        f"/agent/{S1}/keys", data={"key": "1", "screen": FINGERPRINT}, headers=ORIGIN
    )
    assert response.status_code == 200
    assert "banner--unsure" in response.text
    assert "Sent option 1, but the question is still showing." in response.text
    assert "Answered." not in response.text
    assert len(w.herd.writes("agent.send_keys")) == 1, "an unconfirmed key is never re-sent"


def _on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *kinds: str) -> None:
    # The form offers only harnesses on PATH, so the test puts stand-ins there.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for kind in kinds:
        stand_in = bin_dir / kind
        _ = stand_in.write_text("#!/bin/sh\n")
        stand_in.chmod(0o700)
    monkeypatch.setenv("PATH", str(bin_dir))


def test_the_new_agent_form_has_a_way_back_and_a_first_prompt(tmp_path: Path) -> None:
    herd = FakeHerd()
    herd.add_workspace("w1")
    page = web(tmp_path, herd).http.get("/new").text
    assert 'aria-label="Back to herd"' in page
    assert 'href="/"' in page
    assert 'name="prompt"' in page


def test_a_destructive_first_prompt_holds_the_form_and_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on_path(tmp_path, monkeypatch, "claude")
    herd = FakeHerd()
    herd.add_workspace("w1")
    w = web(tmp_path, herd)
    data = {
        "name": "probe",
        "kind": "claude",
        "workspace": "w1",
        "cwd": str(tmp_path),
        "prompt": "git push --force",
    }
    response = w.http.post("/new", data=data, headers=ORIGIN)
    assert response.status_code == 200
    assert "git push --force" in response.text
    assert 'name="confirm" value="1"' in response.text
    assert "Start it anyway" in response.text
    assert herd.writes("tab.create") == []


def test_a_first_prompt_that_was_not_typed_is_said_on_the_page_it_opens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on_path(tmp_path, monkeypatch, "claude")
    herd = FakeHerd()
    herd.add_workspace("w1")
    herd.started_session = S1
    # The new pane never reports it is listening, so the prompt is not typed.
    w = web(tmp_path, herd)
    data = {"name": "probe", "kind": "claude", "workspace": "w1", "cwd": str(tmp_path)}
    response = w.http.post(
        "/new", data={**data, "prompt": "hello"}, headers=ORIGIN, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/agent/{S1}?first=not_ready"
    assert herd.writes("agent.prompt") == []
    page = w.http.get(response.headers["location"]).text
    assert "Started, but your first prompt was not sent." in page
    assert "not accepting input" in page


def test_an_unknown_first_code_says_nothing(tmp_path: Path) -> None:
    w = idle(tmp_path)
    page = w.http.get(f"/agent/{S1}?first=%3Cscript%3E").text
    assert "first prompt" not in page


def test_an_answer_redraws_the_status_from_a_fresh_read(tmp_path: Path) -> None:
    w = blocked(tmp_path)

    def answered(herd: FakeHerd, _params: object) -> None:
        herd.agents[P3]["agent_status"] = "working"

    w.herd.after["agent.send_keys"] = answered
    response = w.http.post(
        f"/agent/{S1}/keys", data={"key": "1", "screen": FINGERPRINT}, headers=ORIGIN
    )
    assert "Answered." in response.text
    assert 'id="agent-status" hx-swap-oob="true">working<' in response.text
    assert 'class="dot dot--working" id="agent-dot" hx-swap-oob="true"' in response.text


def test_an_answer_herdr_has_not_caught_up_with_says_updating(tmp_path: Path) -> None:
    w = blocked(tmp_path)

    def answered(herd: FakeHerd, _params: object) -> None:
        # agent.get has moved on and the snapshot has not caught up with it yet.
        herd.holders[P3] = {**herd.agents[P3], "agent_status": "working"}

    w.herd.after["agent.send_keys"] = answered
    response = w.http.post(
        f"/agent/{S1}/keys", data={"key": "1", "screen": FINGERPRINT}, headers=ORIGIN
    )
    assert "Answered." in response.text
    assert 'id="agent-status" hx-swap-oob="true">updating<' in response.text
    assert "dot--updating" in response.text
    # A poll redraws no status: each extra swapped element is another afterSwap for app.js.
    poll = w.http.get(f"/agent/{S1}/answer").text
    assert "hx-swap-oob" not in poll


def _forms(page: str) -> list[str]:
    return [part.split("</form>", 1)[0] for part in page.split("<form")[1:]]


def test_the_doors_interrupt_confirm_is_its_own_and_never_rides_on_the_prompt(
    tmp_path: Path,
) -> None:
    herd = FakeHerd()
    herd.put(P3, "", status="blocked", launch_pending=True)
    w = web(tmp_path, herd)
    held = w.http.post("/starting/w1:p3/keys", data={"key": "ctrl-c"}, headers=ORIGIN)
    assert held.status_code == 200
    assert herd.writes("agent.send_keys") == []
    forms = _forms(held.text)
    prompt = next(f for f in forms if 'name="text"' in f)
    assert 'name="confirm"' not in prompt, "the interrupt's confirm landed on the prompt form"
    armed = [f for f in forms if 'value="ctrl-c"' in f and 'name="confirm"' in f]
    assert len(armed) == 1, "the held interrupt offers no confirm of its own"
    confirmed = w.http.post(
        "/starting/w1:p3/keys", data={"key": "ctrl-c", "confirm": "1"}, headers=ORIGIN
    )
    assert confirmed.status_code == 200
    assert herd.writes("agent.send_keys") == [{"target": "w1:p3", "keys": ["ctrl-c"]}]


def test_the_doors_destructive_confirm_is_tied_to_the_screen_it_was_asked_about(
    tmp_path: Path,
) -> None:
    herd = FakeHerd()
    herd.put(P3, "", status="idle")
    herd.screens[P3] = [SETTLED]
    w = web(tmp_path, herd)
    held = w.http.post("/starting/w1:p3/prompt", data={"text": "rm -rf build"}, headers=ORIGIN)
    assert held.status_code == 200
    prompt = next(f for f in _forms(held.text) if 'name="text"' in f)
    assert 'name="confirm"' in prompt
    assert f'name="screen" value="{screen_digest(SETTLED)}"' in prompt
    herd.screens[P3] = [SETTLED.replace("Done.", "Done, and moved on.")]
    moved = w.http.post(
        "/starting/w1:p3/prompt",
        data={"text": "rm -rf build", "confirm": "1", "screen": screen_digest(SETTLED)},
        headers=ORIGIN,
    )
    assert moved.status_code == 409
    assert w.herd.writes("agent.prompt") == []
    unbound = w.http.post(
        "/starting/w1:p3/prompt", data={"text": "rm -rf build", "confirm": "1"}, headers=ORIGIN
    )
    assert unbound.status_code == 200, "a confirm that names no screen confirms nothing"
    assert w.herd.writes("agent.prompt") == []


def _exits(herd: FakeHerd, _params: dict[str, JsonValue]) -> None:
    """The write ended the agent: its pane is gone from the herd by the time the page is drawn."""
    _ = herd.agents.pop(P3, None)


def test_an_interrupt_that_ends_the_agent_still_says_it_was_sent(tmp_path: Path) -> None:
    w = blocked(tmp_path)
    w.herd.after["agent.send_keys"] = _exits
    data = {"key": "ctrl-c", "confirm": "1"}
    response = w.http.post(f"/agent/{S1}/keys", data=data, headers=ORIGIN)
    assert response.status_code == 200
    assert "Sent Ctrl+C" in response.text or "You sent Ctrl+C" in response.text


def test_an_exit_prompt_that_ends_the_agent_still_says_it_was_sent(tmp_path: Path) -> None:
    w = idle(tmp_path)
    w.herd.after["agent.prompt"] = _exits
    response = w.http.post(f"/agent/{S1}/prompt", data={"text": "/exit"}, headers=ORIGIN)
    assert response.status_code == 200
    assert "Sent" in response.text
    assert len(w.herd.writes("agent.prompt")) == 1


def test_the_first_prompt_after_answering_a_question_is_not_refused_as_moved(
    tmp_path: Path,
) -> None:
    w = blocked(tmp_path)
    drawn = _posted_back(w.http.get(f"/agent/{S1}/compose").text)
    # A question's screen is no screen to type against, so it claims none.
    assert drawn["screen"] == ""
    asked = w.http.post(f"/agent/{S1}/prompt", data={"text": "hi"}, headers=ORIGIN)
    assert asked.status_code == 409
    assert _posted_back(asked.text)["screen"] == ""
    # The question is answered from the keys; the agent settles; the composer was never redrawn.
    w.herd.put(P3, S1, status="idle")
    w.herd.screens[P3] = [SETTLED, echoed("\u276f hello there")]
    sent = w.http.post(f"/agent/{S1}/prompt", data={**drawn, "text": "hello there"}, headers=ORIGIN)
    assert sent.status_code == 200
    assert "Sent, and it is on the pane." in sent.text
