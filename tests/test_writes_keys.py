"""Tapping a key: only what the freshly read screen offers, at the agent holding the session now."""

from pathlib import Path

import pytest

from herdr_pwa.engine.dialog import BlockedPrompt
from herdr_pwa.engine.transport import EngineUnavailable
from herdr_pwa.jsontypes import JsonObject
from herdr_pwa.writes.answer import KeyAsk, KeySent, send_key, send_starting_key
from herdr_pwa.writes.keys import allowed_keys, answer_digits, control_token, key_ends_question
from herdr_pwa.writes.outcome import Evidence, Outcome
from herdr_pwa.writes.refusal import Refusal, Refused
from tests._fake_herd import P3, P5, S1, S2, SETTLED, FakeHerd, Rig, capture, rig

SEND_KEYS = "agent.send_keys"
PERMISSION = capture("capture-blocked-permission-prompt")
FINGERPRINT = control_token(BlockedPrompt.parse(PERMISSION))


def blocked_rig(tmp_path: Path, screen: str = PERMISSION) -> Rig:
    herd = FakeHerd()
    herd.put(P3, S1, status="blocked")
    herd.screens[P3] = [screen]
    return rig(tmp_path, herd)


def answered(herd: FakeHerd, _params: JsonObject) -> None:
    """What the TUI does with a key that answers: the dialog goes and the agent works."""
    herd.agents[P3]["agent_status"] = "working"


def sent(result: KeySent | Refused) -> KeySent:
    assert isinstance(result, KeySent), result
    return result


def refused(result: KeySent | Refused) -> Refused:
    assert isinstance(result, Refused), result
    return result


def test_a_digit_the_dialog_offers_reaches_the_pane_holding_the_session(tmp_path: Path) -> None:
    r = blocked_rig(tmp_path)
    r.herd.after[SEND_KEYS] = answered
    result = sent(send_key(r.writer, KeyAsk(S1, "1", drawn_from=FINGERPRINT)))
    assert r.herd.writes(SEND_KEYS) == [{"target": "w1:p3", "keys": ["1"]}]
    assert (result.outcome, result.evidence) == (Outcome.CONFIRMED, Evidence.GONE)
    assert result.ended
    assert result.prompt is None, "a question that went away leaves no live controls"
    results = [rec.get("result") for rec in r.audit_records if rec["phase"] == "outcome"]
    assert results == ["confirmed:gone"]


def test_an_enter_that_was_acked_but_did_nothing_is_unconfirmed_and_not_resent(
    tmp_path: Path,
) -> None:
    """The key half of the taps-do-not-register report: an ack is bytes taken, not an act."""
    r = blocked_rig(tmp_path)
    result = sent(send_key(r.writer, KeyAsk(S1, "enter", drawn_from=FINGERPRINT)))
    assert (result.outcome, result.evidence) == (Outcome.UNCONFIRMED, Evidence.UNMOVED)
    assert len(r.herd.writes(SEND_KEYS)) == 1
    assert result.prompt is not None
    assert control_token(result.prompt) == FINGERPRINT


def test_a_key_into_a_recycled_pane_is_refused_on_the_wire(tmp_path: Path) -> None:
    """The misroute: a tap must never land in another agent's input box."""
    r = blocked_rig(tmp_path)
    impostor = dict(r.herd.agents[P3])
    impostor["agent_session"] = {"agent": "claude", "kind": "id", "source": "x", "value": S2}
    r.herd.holders[P3] = impostor
    result = refused(send_key(r.writer, KeyAsk(S1, "1", drawn_from=FINGERPRINT)))
    assert result.refusal is Refusal.SESSION_MOVED
    assert r.herd.writes(SEND_KEYS) == []


def test_a_moved_agent_gets_the_key_at_its_new_pane_not_the_posted_one(tmp_path: Path) -> None:
    herd = FakeHerd()
    herd.put(P5, S1, status="blocked")
    herd.put(P3, S2, status="blocked")
    herd.screens[P5] = [PERMISSION]
    herd.screens[P3] = [capture("capture-blocked-trust-folder")]
    r = rig(tmp_path, herd)
    _ = sent(send_key(r.writer, KeyAsk(S1, "esc", expected_target=P3, drawn_from=FINGERPRINT)))
    assert [w["target"] for w in herd.writes(SEND_KEYS)] == ["w1:p5"]


@pytest.mark.parametrize("key", ["7", "x", "ctrl-d", ""])
def test_a_key_the_screen_does_not_offer_is_refused(tmp_path: Path, key: str) -> None:
    r = blocked_rig(tmp_path)
    result = refused(send_key(r.writer, KeyAsk(S1, key, drawn_from=FINGERPRINT)))
    assert (result.refusal, result.status) == (Refusal.KEY_NOT_OFFERED, 400)
    assert r.herd.writes(SEND_KEYS) == []


def test_controls_drawn_from_another_screen_are_refused_with_the_current_one(
    tmp_path: Path,
) -> None:
    r = blocked_rig(tmp_path)
    result = refused(send_key(r.writer, KeyAsk(S1, "1", drawn_from="old")))
    assert (result.refusal, result.status) == (Refusal.SCREEN_CHANGED, 409)
    assert result.screen == FINGERPRINT
    assert result.prompt is not None
    assert r.herd.writes(SEND_KEYS) == []


def test_interrupt_takes_two_taps_and_skips_the_fingerprint(tmp_path: Path) -> None:
    r = blocked_rig(tmp_path)
    first = refused(send_key(r.writer, KeyAsk(S1, "ctrl-c", drawn_from="moving")))
    assert first.held
    assert r.herd.writes(SEND_KEYS) == []
    _ = sent(send_key(r.writer, KeyAsk(S1, "ctrl-c", drawn_from="moving", confirmed=True)))
    assert r.herd.writes(SEND_KEYS) == [{"target": "w1:p3", "keys": ["ctrl-c"]}]


def test_interrupt_works_when_the_question_cannot_be_read(tmp_path: Path) -> None:
    r = blocked_rig(tmp_path)
    r.herd.fail["agent.read"] = EngineUnavailable("down")
    blind = refused(send_key(r.writer, KeyAsk(S1, "enter")))
    assert blind.refusal is Refusal.UNREADABLE
    _ = sent(send_key(r.writer, KeyAsk(S1, "ctrl-c", confirmed=True)))
    assert len(r.herd.writes(SEND_KEYS)) == 1


def test_interrupt_is_still_refused_while_launching(tmp_path: Path) -> None:
    herd = FakeHerd()
    herd.put(P3, S1, status="idle", launch_pending=True)
    herd.screens[P3] = [SETTLED]
    r = rig(tmp_path, herd)
    result = refused(send_key(r.writer, KeyAsk(S1, "ctrl-c", confirmed=True)))
    assert result.refusal is Refusal.NOT_READY


def test_a_working_agent_takes_pad_keys_without_a_fingerprint(tmp_path: Path) -> None:
    herd = FakeHerd()
    herd.put(P3, S1, status="working")
    r = rig(tmp_path, herd)
    result = sent(send_key(r.writer, KeyAsk(S1, "esc")))
    assert (result.outcome, result.evidence) == (Outcome.ACCEPTED, Evidence.DELIVERED)
    assert herd.writes("agent.read") == [], "nothing is waiting, so nothing is read for digits"


def test_a_navigation_key_redraws_the_controls_from_the_screen_it_made(tmp_path: Path) -> None:
    moved = PERMISSION.replace(" \u276f 1. Yes\n   2.", "   1. Yes\n \u276f 2.")
    r = blocked_rig(tmp_path)
    r.herd.screens[P3] = [PERMISSION, moved]
    result = sent(send_key(r.writer, KeyAsk(S1, "down", drawn_from=FINGERPRINT)))
    assert result.prompt is not None
    # The cursor moved inside one question, so the redrawn controls still take the next tap.
    again = send_key(r.writer, KeyAsk(S1, "down", drawn_from=control_token(result.prompt)))
    assert isinstance(again, KeySent)


def test_a_starting_agent_stopped_at_a_trust_dialog_takes_pad_keys_by_address(
    tmp_path: Path,
) -> None:
    herd = FakeHerd()
    herd.put(P3, "", status="blocked", launch_pending=True)
    r = rig(tmp_path, herd)
    _ = sent(send_starting_key(r.writer, P3, "enter"))
    assert herd.writes(SEND_KEYS) == [{"target": "w1:p3", "keys": ["enter"]}]
    assert refused(send_starting_key(r.writer, P3, "1")).refusal is Refusal.KEY_NOT_OFFERED


def test_a_starting_key_at_an_agent_that_has_a_session_goes_to_its_own_page(
    tmp_path: Path,
) -> None:
    r = blocked_rig(tmp_path)
    assert refused(send_starting_key(r.writer, P3, "enter")).refusal is Refusal.HAS_IDENTITY


def test_a_starting_key_where_nothing_is_waiting_is_refused(tmp_path: Path) -> None:
    herd = FakeHerd()
    herd.put(P3, "", status="idle")
    r = rig(tmp_path, herd)
    assert refused(send_starting_key(r.writer, P3, "enter")).refusal is Refusal.NOT_WAITING


def test_digits_are_offered_only_for_a_live_single_choice_menu_printed_whole() -> None:
    assert answer_digits(BlockedPrompt.parse(PERMISSION)) == ("1", "2", "3")
    wrapped = BlockedPrompt.parse(capture("capture-blocked-permission-prompt-wrapped"))
    assert answer_digits(wrapped) == ()
    checklist = BlockedPrompt.parse(capture("capture-blocked-multiselect-checklist"))
    assert answer_digits(checklist) == ()
    assert answer_digits(None) == ()


def test_the_allowed_set_is_the_pad_the_footer_and_earned_digits() -> None:
    assert allowed_keys(None) == {"esc", "up", "down", "enter", "ctrl-c"}
    assert allowed_keys(BlockedPrompt.parse(PERMISSION)) == {
        "esc", "up", "down", "enter", "ctrl-c", "tab", "ctrl-e", "1", "2", "3",
    }  # fmt: skip


def test_which_keys_end_a_question() -> None:
    prompt = BlockedPrompt.parse(PERMISSION)
    assert key_ends_question(prompt, "1")
    assert key_ends_question(prompt, "enter")
    assert not key_ends_question(prompt, "down")
    assert key_ends_question(None, "ctrl-c")


def test_a_status_line_ticking_does_not_make_a_still_question_stale(tmp_path: Path) -> None:
    """The raw screen moved (an animated bullet) and the question did not: the tap goes through."""
    r = blocked_rig(tmp_path)
    r.herd.screens[P3] = [PERMISSION.replace("\u25cf I'll run that.", "\u25d0 I'll run that.")]
    drawn = FINGERPRINT
    assert (
        BlockedPrompt.parse(r.herd.screens[P3][0]).fingerprint
        != BlockedPrompt.parse(PERMISSION).fingerprint
    )
    result = send_key(r.writer, KeyAsk(S1, "esc", drawn_from=drawn))
    assert isinstance(result, KeySent), result


def test_a_key_drawn_under_a_question_that_has_gone_is_refused(tmp_path: Path) -> None:
    """Esc drawn under a question, the question answered elsewhere: Esc now would interrupt."""
    herd = FakeHerd()
    herd.put(P3, S1, status="working")
    r = rig(tmp_path, herd)
    result = refused(send_key(r.writer, KeyAsk(S1, "esc", drawn_from=FINGERPRINT)))
    assert result.refusal is Refusal.SCREEN_CHANGED
    assert result.prompt is None
    assert herd.writes(SEND_KEYS) == []
    assert r.audit_records == [], "a refusal the app makes on its own is not recorded"


def test_an_interrupt_drawn_under_a_question_that_has_gone_still_goes(tmp_path: Path) -> None:
    herd = FakeHerd()
    herd.put(P3, S1, status="working")
    r = rig(tmp_path, herd)
    _ = sent(send_key(r.writer, KeyAsk(S1, "ctrl-c", drawn_from=FINGERPRINT, confirmed=True)))
    assert herd.writes(SEND_KEYS) == [{"target": "w1:p3", "keys": ["ctrl-c"]}]


def _lost_after_send(_herd: FakeHerd, _params: JsonObject) -> None:
    """herdr typed the key, then the connection dropped before its answer came back."""
    lost = EngineUnavailable("the runner closed the connection while answering")
    lost.after_send = True
    raise lost


def test_a_key_whose_answer_was_lost_is_said_to_maybe_have_gone(tmp_path: Path) -> None:
    r = blocked_rig(tmp_path)
    r.herd.after[SEND_KEYS] = _lost_after_send
    result = refused(send_key(r.writer, KeyAsk(S1, "1", drawn_from=FINGERPRINT)))
    assert result.refusal is Refusal.ANSWER_LOST
    assert "may or may not have been sent" in result.says
    assert len(r.herd.writes(SEND_KEYS)) == 1, "never re-sent"
    results = [rec.get("result") for rec in r.audit_records if rec["phase"] == "outcome"]
    assert results == ["unknown:EngineUnavailable"]


def test_a_second_tab_answering_while_the_first_is_mid_write_is_refused(tmp_path: Path) -> None:
    """Two phones on one question: both read it, both pass the token check, one may send."""
    r = blocked_rig(tmp_path)
    r.herd.after[SEND_KEYS] = answered
    second: list[KeySent | Refused] = []

    def other_tab(herd: FakeHerd, params: JsonObject) -> None:
        # Runs inside the first tab's write, right after it read the question to check against.
        del herd.after["agent.read"], params
        second.append(send_key(r.writer, KeyAsk(S1, "1", drawn_from=FINGERPRINT)))

    r.herd.after["agent.read"] = other_tab
    first = sent(send_key(r.writer, KeyAsk(S1, "1", drawn_from=FINGERPRINT)))
    [late] = second
    assert (refused(late).refusal, refused(late).status) == (Refusal.IN_FLIGHT, 409)
    assert r.herd.writes(SEND_KEYS) == [{"target": "w1:p3", "keys": ["1"]}]
    assert first.evidence is Evidence.GONE
    # Once the first write has finished, the session takes writes again.
    again = refused(send_key(r.writer, KeyAsk(S1, "1", drawn_from=FINGERPRINT)))
    assert again.refusal is not Refusal.IN_FLIGHT
