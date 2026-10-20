"""Tapping a key: only what the freshly read screen offers, at the agent holding the session now."""

import itertools
from pathlib import Path

import pytest
from pydantic import JsonValue

from herdr_pwa.decisions import answer_digits as decided_digits
from herdr_pwa.decisions import decide_screen
from herdr_pwa.engine import BlockedPrompt, EngineUnavailable, KeyHint
from herdr_pwa.panetext import as_text_read
from herdr_pwa.writes.answer import KeyAsk, KeySent, StartingKeyAsk, send_key, send_starting_key
from herdr_pwa.writes.keys import (
    allowed_keys,
    answer_digits,
    control_token,
    key_ends_question,
    pad_keys,
)
from herdr_pwa.writes.outcome import (
    ANSWER_BUDGET,
    ANSWER_INTERVAL,
    MOVE_BUDGET,
    MOVE_INTERVAL,
    UNREADABLE,
    Evidence,
    Outcome,
    Pacing,
    Poll,
    Watched,
    watch,
)
from herdr_pwa.writes.refusal import Refusal, Refused
from tests._fake_herd import (
    FIXTURES,
    P3,
    P5,
    S1,
    S2,
    SETTLED,
    FakeClock,
    FakeHerd,
    Rig,
    capture,
    rig,
)

SEND_KEYS = "agent.send_keys"
PERMISSION = capture("capture-blocked-permission-prompt")
FINGERPRINT = control_token(BlockedPrompt.parse(PERMISSION))


def blocked_rig(tmp_path: Path, screen: str = PERMISSION) -> Rig:
    herd = FakeHerd()
    herd.put(P3, S1, status="blocked")
    herd.screens[P3] = [screen]
    return rig(tmp_path, herd)


def answered(herd: FakeHerd, _params: dict[str, JsonValue]) -> None:
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


@pytest.mark.parametrize("drawn_from", ["", "  "])
def test_a_key_sent_with_no_screen_token_is_refused_as_stale(
    tmp_path: Path, drawn_from: str
) -> None:
    """A control that carries no fingerprint proves no question, so it is never let through."""
    r = blocked_rig(tmp_path)
    result = refused(send_key(r.writer, KeyAsk(S1, "1", drawn_from=drawn_from)))
    assert (result.refusal, result.status) == (Refusal.SCREEN_CHANGED, 409)
    assert result.screen == FINGERPRINT
    assert r.herd.writes(SEND_KEYS) == []


def test_interrupt_takes_two_taps_and_skips_the_fingerprint(tmp_path: Path) -> None:
    r = blocked_rig(tmp_path)
    first = refused(send_key(r.writer, KeyAsk(S1, "ctrl-c", drawn_from="moving")))
    assert first.held
    assert r.herd.writes(SEND_KEYS) == []
    _ = sent(send_key(r.writer, KeyAsk(S1, "ctrl-c", drawn_from="moving", confirmed=True)))
    assert r.herd.writes(SEND_KEYS) == [{"target": "w1:p3", "keys": ["ctrl-c"]}]


def test_a_second_tab_s_interrupt_is_refused_mid_interrupt_and_taken_after_it(
    tmp_path: Path,
) -> None:
    # Refused only while the first is out: past it, a second deliberate Ctrl+C is a stubborn
    # process's usual remedy, and every interrupt is already two taps.
    r = blocked_rig(tmp_path)
    interrupt = KeyAsk(S1, "ctrl-c", confirmed=True)
    late: list[KeySent | Refused] = []

    def other_tab(herd: FakeHerd, params: dict[str, JsonValue]) -> None:
        del herd.after[SEND_KEYS], params
        late.append(send_key(r.writer, interrupt))

    r.herd.after[SEND_KEYS] = other_tab
    _ = sent(send_key(r.writer, interrupt))
    [mid] = late
    assert refused(mid).refusal is Refusal.IN_FLIGHT
    _ = sent(send_key(r.writer, interrupt))
    assert r.herd.writes(SEND_KEYS) == [{"target": "w1:p3", "keys": ["ctrl-c"]}] * 2


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
    result = sent(send_key(r.writer, KeyAsk(S1, "down")))
    assert (result.outcome, result.evidence) == (Outcome.ACCEPTED, Evidence.DELIVERED)
    assert herd.writes("agent.read") == [], "nothing is waiting, so nothing is read for digits"


def test_esc_stops_a_working_agents_turn_so_it_takes_two_taps(tmp_path: Path) -> None:
    herd = FakeHerd()
    herd.put(P3, S1, status="working")
    r = rig(tmp_path, herd)
    first = refused(send_key(r.writer, KeyAsk(S1, "esc")))
    assert (first.refusal, first.held) == (Refusal.CONFIRM, True)
    assert herd.writes(SEND_KEYS) == []
    _ = sent(send_key(r.writer, KeyAsk(S1, "esc", confirmed=True)))
    assert herd.writes(SEND_KEYS) == [{"target": "w1:p3", "keys": ["esc"]}]


def test_esc_at_a_question_declines_it_in_one_tap(tmp_path: Path) -> None:
    r = blocked_rig(tmp_path)
    _ = sent(send_key(r.writer, KeyAsk(S1, "esc", drawn_from=FINGERPRINT)))
    assert r.herd.writes(SEND_KEYS) == [{"target": "w1:p3", "keys": ["esc"]}]


def test_esc_at_an_idle_prompt_takes_one_tap(tmp_path: Path) -> None:
    herd = FakeHerd()
    herd.put(P3, S1, status="idle")
    herd.screens[P3] = [SETTLED]
    r = rig(tmp_path, herd)
    _ = sent(send_key(r.writer, KeyAsk(S1, "esc")))
    assert herd.writes(SEND_KEYS) == [{"target": "w1:p3", "keys": ["esc"]}]


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
    _ = sent(send_starting_key(r.writer, StartingKeyAsk(P3, "enter")))
    assert herd.writes(SEND_KEYS) == [{"target": "w1:p3", "keys": ["enter"]}]
    assert (
        refused(send_starting_key(r.writer, StartingKeyAsk(P3, "1"))).refusal
        is Refusal.KEY_NOT_OFFERED
    )


def test_a_starting_key_at_an_agent_that_has_a_session_goes_to_its_own_page(
    tmp_path: Path,
) -> None:
    r = blocked_rig(tmp_path)
    assert (
        refused(send_starting_key(r.writer, StartingKeyAsk(P3, "enter"))).refusal
        is Refusal.HAS_IDENTITY
    )


def test_a_starting_key_where_nothing_is_waiting_is_refused(tmp_path: Path) -> None:
    herd = FakeHerd()
    herd.put(P3, "", status="idle")
    r = rig(tmp_path, herd)
    assert (
        refused(send_starting_key(r.writer, StartingKeyAsk(P3, "enter"))).refusal
        is Refusal.NOT_WAITING
    )


def test_digits_are_offered_only_for_a_live_single_choice_menu_printed_whole() -> None:
    assert answer_digits(BlockedPrompt.parse(PERMISSION)) == ("1", "2", "3")
    wrapped = BlockedPrompt.parse(capture("capture-blocked-permission-prompt-wrapped"))
    assert answer_digits(wrapped) == ()
    checklist = BlockedPrompt.parse(capture("capture-blocked-multiselect-checklist"))
    assert answer_digits(checklist) == ()
    assert answer_digits(None) == ()


def _vendored(name: str) -> BlockedPrompt:
    """A vendored claude screen, read as the app reads a colour capture."""
    raw = (FIXTURES / "foreign" / "collie" / f"claude--{name}.txt").read_text(encoding="utf-8")
    return BlockedPrompt.from_reads(as_text_read(raw), lambda: raw)


def test_rows_beside_a_preview_earn_no_digit_and_every_printed_key_stays() -> None:
    """Measured live: there a digit only moves the cursor, and Enter is what answers."""
    preview = _vendored("select-preview")
    assert [o.label for o in preview.options] == ["Boxy", "Rounded", "Minimal"]
    assert answer_digits(preview) == ()
    assert decided_digits(preview) == ()
    assert allowed_keys(preview) == {"esc", "up", "down", "enter", "ctrl-c"}
    assert {h.key for h in decide_screen(preview).answer_hints} == {"enter", "up", "down", "esc"}


def test_a_plain_question_and_a_checklist_still_answer_on_their_digits() -> None:
    plain = _vendored("select-menu")
    assert answer_digits(plain) == decided_digits(plain) == ("1", "2", "3", "4", "5")
    assert key_ends_question(plain, "2")
    checklist = BlockedPrompt.parse(capture("capture-blocked-multiselect-checklist"))
    assert decided_digits(checklist) == ("1", "2", "3", "4", "5", "6")


def test_the_allowed_set_is_the_pad_the_footer_and_earned_digits() -> None:
    assert allowed_keys(None) == {"esc", "up", "down", "enter", "ctrl-c"}
    assert allowed_keys(BlockedPrompt.parse(PERMISSION)) == {
        "esc", "up", "down", "enter", "ctrl-c", "tab", "ctrl-e", "1", "2", "3",
    }  # fmt: skip


def test_a_key_that_hands_the_terminal_to_another_program_is_never_allowed() -> None:
    """Ctrl+G on claude's plan approval opens Vim in the pane, which no key here can leave."""
    plan = BlockedPrompt.parse(capture("capture-blocked-plan-approval"))
    assert "ctrl-g" not in allowed_keys(plan)
    assert "1" in allowed_keys(plan)
    # A hand-off named on a pad key takes that key off the pad too: the screen said what it does.
    vim_on_enter = plan.model_copy(update={"handoffs": (KeyHint(key="enter", verb="edit in vim"),)})
    assert "enter" not in allowed_keys(vim_on_enter)


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


def _lost_after_send(_herd: FakeHerd, _params: dict[str, JsonValue]) -> None:
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

    def other_tab(herd: FakeHerd, params: dict[str, JsonValue]) -> None:
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


# The watcher's endings, over a scripted run of reads.

ASKED = BlockedPrompt.parse(PERMISSION)
OTHER = BlockedPrompt.parse(capture("capture-blocked-plan-approval"))


def _watch(*reads: Poll, ends: bool = True) -> tuple[Watched, FakeClock]:
    clock = FakeClock()
    queue = list(reads)

    def read() -> Poll:
        return queue.pop(0) if len(queue) > 1 else queue[0]

    pacing = Pacing(sleep=clock.sleep, clock=clock)
    return watch(read, aimed=ASKED.identity, ends=ends, pacing=pacing), clock


def test_one_frame_of_another_question_is_not_believed_to_be_a_move() -> None:
    seen, _ = _watch(OTHER, ASKED)
    assert (seen.outcome, seen.evidence) == (Outcome.UNCONFIRMED, Evidence.UNMOVED)
    assert seen.prompt is not None
    assert seen.prompt.identity == ASKED.identity


def test_a_move_is_believed_on_its_second_frame_one_interval_later() -> None:
    seen, clock = _watch(OTHER, OTHER, ASKED)
    assert (seen.outcome, seen.evidence, seen.polls) == (Outcome.UNCONFIRMED, Evidence.MOVED, 2)
    assert seen.prompt is not None
    assert seen.prompt.identity == OTHER.identity
    assert clock.slept == [ANSWER_INTERVAL], "one interval, never the whole budget"


def test_a_navigation_key_that_moves_on_is_confirmed_on_two_frames() -> None:
    seen, clock = _watch(OTHER, ends=False)
    assert (seen.outcome, seen.evidence, seen.polls) == (Outcome.CONFIRMED, Evidence.MOVED, 2)
    assert clock.slept == [MOVE_INTERVAL]


def test_a_dialog_herdr_lags_on_is_confirmed_once_herdr_catches_up() -> None:
    # Measured on hermes: blocked with no dialog on screen for a few reads, then a stop.
    seen, _ = _watch(UNREADABLE, UNREADABLE, UNREADABLE, None)
    assert (seen.outcome, seen.evidence) == (Outcome.CONFIRMED, Evidence.GONE)
    assert seen.polls == 5, "gone still takes two reads in a row"


def test_a_dialog_back_after_an_unreadable_frame_is_not_confirmed() -> None:
    seen, _ = _watch(UNREADABLE, ASKED)
    assert (seen.outcome, seen.evidence) == (Outcome.UNCONFIRMED, Evidence.UNMOVED)
    assert seen.prompt is not None
    assert seen.prompt.identity == ASKED.identity


def test_a_screen_that_stays_unreadable_is_accepted_at_the_budget_and_claims_nothing() -> None:
    seen, clock = _watch(UNREADABLE)
    assert (seen.outcome, seen.evidence, seen.prompt) == (Outcome.ACCEPTED, Evidence.UNREAD, None)
    assert sum(clock.slept) >= ANSWER_BUDGET, "watched to the end of the budget"


def _changing(n: int) -> BlockedPrompt:
    return BlockedPrompt.parse(f"───\nStep {n}: which one?\n\n\u276f 1. Yes\n  2. No\n")


@pytest.mark.parametrize("ends", [True, False])
def test_a_screen_that_shows_a_new_question_on_every_read_still_ends(ends: bool) -> None:
    """Each frame is one uncorroborated move, so only a hard stop ends the watch."""
    clock = FakeClock()
    reads = itertools.count()

    def read() -> Poll:
        n = next(reads)
        if n > 200:
            raise AssertionError("the watch kept polling an ever-moving screen")
        return _changing(n)

    interval, budget = (ANSWER_INTERVAL, ANSWER_BUDGET) if ends else (MOVE_INTERVAL, MOVE_BUDGET)
    seen = watch(read, aimed=ASKED.identity, ends=ends, pacing=Pacing(clock.sleep, clock))
    assert seen.outcome is (Outcome.UNCONFIRMED if ends else Outcome.ACCEPTED)
    # The look that finds the deadline passed, then one grace look an interval later.
    reached = next(n for n in itertools.count(1) if n * interval >= budget - 1e-9)
    assert clock.slept == [interval] * (reached + 1), "one grace look past the deadline, no more"


# The opencode permission dialog: buttons answered sideways, offered from what
# the screen itself prints.

OC_PERMISSION = capture("capture-blocked-opencode-permission")


def test_the_buttons_pad_offers_the_arrows_the_screen_uses() -> None:
    prompt = BlockedPrompt.parse(
        OC_PERMISSION, ansi=capture_ansi("capture-blocked-opencode-permission")
    )
    assert prompt.shape.value == "buttons"
    assert pad_keys(prompt) == ("esc", "left", "right", "enter")
    # The rows are addressed by the focus, so no digit is ever offered.
    assert answer_digits(prompt) == ()
    assert allowed_keys(prompt) == frozenset({"esc", "left", "right", "enter", "ctrl-c"})


def test_up_and_down_do_not_count_as_answers_on_a_buttons_screen() -> None:
    prompt = BlockedPrompt.parse(OC_PERMISSION)
    assert key_ends_question(prompt, "enter") is True
    assert key_ends_question(prompt, "esc") is True
    assert key_ends_question(prompt, "left") is False
    assert key_ends_question(prompt, "right") is False
    assert key_ends_question(prompt, "up") is False


def capture_ansi(name: str) -> str:
    return (FIXTURES / name / "visible.ansi").read_text(encoding="utf-8")


def _unasked_run(label: str) -> BlockedPrompt:
    return BlockedPrompt.parse(f"───\nPick one\n\n\u276f 1. {label}\n  2. Delete\n")


def test_relabelling_a_row_no_option_names_still_moves_the_token() -> None:
    """A cursor run with no question names no options, yet Enter answers it on the cursor's row."""
    kept = _unasked_run("Keep")
    assert (kept.options, "enter" in allowed_keys(kept)) == ((), True)
    assert control_token(kept) != control_token(_unasked_run("Wipe"))
    # A counter ticking inside a label is the same question.
    assert control_token(_unasked_run("Keep (3s)")) == control_token(_unasked_run("Keep (2s)"))


def _menu(rows: int) -> BlockedPrompt:
    listed = "\n".join(f"{'\u276f' if n == 1 else ' '} {n}. Option {n}" for n in range(1, rows + 1))
    footer = "Enter to select · ↑/↓ to navigate · Esc to cancel"
    return BlockedPrompt.parse(f"───\nWhich one should I use?\n\n{listed}\n\n{footer}\n")


def test_a_menu_past_nine_rows_earns_no_digit_at_all() -> None:
    """A tenth row's digit is two keystrokes, and the first one answers row 1."""
    assert answer_digits(_menu(9)) == tuple(str(n) for n in range(1, 10))
    ten = _menu(10)
    assert (ten.shape.value, len(ten.options)) == ("single-choice", 10)
    assert answer_digits(ten) == ()
    assert not {"1", "10"} & allowed_keys(ten)


def test_an_empty_key_is_never_allowed_even_when_a_hint_names_one() -> None:
    blank = BlockedPrompt.parse(PERMISSION).model_copy(
        update={"hints": (KeyHint(key="", verb="select"),)}
    )
    assert "" not in allowed_keys(blank)


def test_a_row_inside_a_stepper_moves_to_the_next_step_rather_than_ending_it() -> None:
    wizard = _vendored("wizard-q1")
    assert wizard.stepper is not None
    assert "1" in answer_digits(wizard)
    assert not key_ends_question(wizard, "1")
    assert not key_ends_question(wizard, "enter")


@pytest.mark.parametrize("typed", ["ENTER", " 1 ", "Esc"])
def test_a_key_is_matched_case_and_space_blind(tmp_path: Path, typed: str) -> None:
    r = blocked_rig(tmp_path)
    _ = sent(send_key(r.writer, KeyAsk(S1, typed, drawn_from=FINGERPRINT)))
    assert r.herd.writes(SEND_KEYS) == [{"target": "w1:p3", "keys": [typed.strip().lower()]}]
