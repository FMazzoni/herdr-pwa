"""A dialog herdr calls a stop: the screen still asks, so it needs you and its taps are taken.

Measured live: hermes's security approval puts a scan report under its options, and when the
report runs long it pushes every word herdr's hermes rule looks for out of the bottom lines the
rule reads. herdr then reports the agent done for the whole life of the dialog.
"""

from pathlib import Path

import pytest
from pydantic import JsonValue

from herdr_pwa.decisions import Overlay, asks_now, read_status, screen_asks
from herdr_pwa.engine import AgentStatus, BlockedPrompt, MenuShape
from herdr_pwa.writes.answer import KeyAsk, KeySent, send_key
from herdr_pwa.writes.keys import answer_digits, control_token
from herdr_pwa.writes.outcome import Evidence, Outcome
from herdr_pwa.writes.refusal import Refusal, Refused
from tests._fake_herd import FIXTURES, P3, S1, SETTLED, FakeHerd, Rig, capture, rig
from tests.test_notifier_loop import build_loop
from tests.webfakes import (
    IDLE_SESSION,
    READ,
    SNAPSHOT,
    WORKING_PANE,
    agents_of,
    build_rig,
    read_frame,
    snapshot_frame,
)

# The approval as herdr's detection read returned it while herdr said `done`.
APPROVAL = (FIXTURES / "hermes" / "stopped-approval.txt").read_text()
TOKEN = control_token(BlockedPrompt.parse(APPROVAL))
SEND_KEYS = "agent.send_keys"
FRAGMENT = {"HX-Request": "true"}
IDLE_PANE = "w652ae557d5b9c1:p2"


# What counts as a screen that still asks


def test_the_approval_herdr_missed_is_read_as_asking() -> None:
    assert screen_asks(BlockedPrompt.parse(APPROVAL))


@pytest.mark.parametrize(
    "screen",
    [
        capture("capture-idle-numbered-recap"),
        capture("capture-idle-claude-prompt"),
        capture("capture-working-claude"),
        SETTLED,
        "",
    ],
)
def test_a_stopped_screen_with_no_dialog_does_not_ask(screen: str) -> None:
    # A numbered list at the end of a finished turn has no keys under it to answer with.
    assert not screen_asks(BlockedPrompt.parse(screen))


# The idle capture with a git segment in its status line: '?' marks untracked files.
DIRTY_STATUS = capture("capture-idle-claude-prompt").replace(
    "herdr-pwa [main]", "herdr-pwa on main [!?]"
)


@pytest.mark.parametrize("status", [AgentStatus.DONE, AgentStatus.IDLE])
def test_a_question_mark_in_an_idle_status_line_is_not_a_dialog_asking(
    status: AgentStatus,
) -> None:
    prompt = BlockedPrompt.parse(DIRTY_STATUS)
    assert "[!?]" in prompt.raw, "the premise: the status line carries the mark"
    assert not prompt.asked
    assert not prompt.identity
    assert not screen_asks(prompt)
    assert not asks_now(status, prompt)


@pytest.mark.parametrize("status", [AgentStatus.DONE, AgentStatus.IDLE])
def test_a_stop_whose_screen_asks_needs_you(status: AgentStatus) -> None:
    asking = Overlay(unread={IDLE_SESSION: False}, asking=frozenset({IDLE_SESSION}))
    assert read_status(status, IDLE_SESSION, asking) is AgentStatus.BLOCKED
    assert read_status(status, "another", asking) is not AgentStatus.BLOCKED


def test_a_working_agent_is_not_promoted_by_what_its_screen_last_showed() -> None:
    asking = Overlay(asking=frozenset({IDLE_SESSION}))
    assert read_status(AgentStatus.WORKING, IDLE_SESSION, asking) is AgentStatus.WORKING


# The tap


def _stopped_at_approval(tmp_path: Path, status: str = "done") -> Rig:
    herd = FakeHerd()
    herd.put(P3, S1, status=status)
    herd.screens[P3] = [APPROVAL]
    return rig(tmp_path, herd)


def _denied(herd: FakeHerd, _params: dict[str, JsonValue]) -> None:
    """What hermes does with `4`: the approval goes, `-> denied` is printed, the turn ends."""
    herd.screens[P3] = [SETTLED]


@pytest.mark.parametrize("status", ["done", "idle"])
def test_a_digit_at_an_approval_herdr_calls_stopped_reaches_the_pane(
    tmp_path: Path, status: str
) -> None:
    r = _stopped_at_approval(tmp_path, status)
    r.herd.after[SEND_KEYS] = _denied
    result = send_key(r.writer, KeyAsk(S1, "4", drawn_from=TOKEN))
    assert isinstance(result, KeySent), result
    assert r.herd.writes(SEND_KEYS) == [{"target": "w1:p3", "keys": ["4"]}]
    assert (result.outcome, result.evidence) == (Outcome.CONFIRMED, Evidence.GONE)
    assert result.ended


def test_an_enter_at_it_is_watched_until_the_dialog_goes(tmp_path: Path) -> None:
    r = _stopped_at_approval(tmp_path)
    # Still up after the key: an ack is bytes taken, and herdr's `done` is not an answer.
    result = send_key(r.writer, KeyAsk(S1, "enter", drawn_from=TOKEN))
    assert isinstance(result, KeySent), result
    assert (result.outcome, result.evidence) == (Outcome.UNCONFIRMED, Evidence.UNMOVED)


def test_a_digit_at_a_stopped_screen_with_no_dialog_is_still_refused(tmp_path: Path) -> None:
    herd = FakeHerd()
    herd.put(P3, S1, status="done")
    herd.screens[P3] = [capture("capture-idle-numbered-recap")]
    result = send_key(rig(tmp_path, herd).writer, KeyAsk(S1, "1"))
    assert isinstance(result, Refused), result
    assert result.refusal is Refusal.KEY_NOT_OFFERED
    assert herd.writes(SEND_KEYS) == []


# Every surface


def _at(pane: str) -> dict[str, JsonValue]:
    return read_frame(APPROVAL, pane=pane)


def _done_frame() -> dict[str, JsonValue]:
    frame = snapshot_frame()
    for raw in agents_of(frame):
        if raw.get("pane_id") == IDLE_PANE:
            raw["agent_status"] = "done"
            raw["revision"] = 9
    return frame


def test_the_feed_the_page_and_the_answer_region_all_say_it_needs_you(tmp_path: Path) -> None:
    replies = {
        SNAPSHOT: _done_frame(),
        READ: read_frame("> "),
        f"{READ} {IDLE_PANE}": _at(IDLE_PANE),
    }
    app = build_rig(tmp_path, replies)
    feed = app.http.get("/feed", headers=FRAGMENT).text
    needs_you = feed.split('class="group group--blocked"', 1)[1].split("</section>", 1)[0]
    assert f"/agent/{IDLE_SESSION}" in needs_you
    answer = app.http.get(f"/agent/{IDLE_SESSION}/answer").text
    assert 'data-hp-status="blocked"' in answer
    for digit in "1234":
        assert f'name="key" value="{digit}"' in answer
    assert f'value="{TOKEN}"' in answer


def test_it_pushes_needs_you(tmp_path: Path) -> None:
    loop = build_loop(tmp_path)
    loop.wire.replies[f"{READ} {WORKING_PANE}"] = _at(WORKING_PANE)
    loop.subscribe()
    _ = loop.notifier.tick()
    loop.set_status("done")
    _ = loop.notifier.tick()
    events = [payload["event"] for _, payload in loop.sender.sent]
    assert events == ["blocked"]


# A countdown is not a new screen

# hermes's slash-command confirm: herdr flags it blocked, it numbers its rows `[1]`, and it
# ticks a countdown under them every second, with clocks in the status bar below that.
SLASH_CONFIRM = (FIXTURES / "hermes" / "blocked-slash-confirm.txt").read_text()


def _ticked(screen: str) -> str:
    """The same dialog a few seconds on: its countdown and the status bar's clock moved."""
    return screen.replace("(19s)", "(13s)").replace("✓ 3m", "✓ 4m")


def test_the_bracketed_rows_are_the_options_and_each_earns_its_digit() -> None:
    prompt = BlockedPrompt.parse(SLASH_CONFIRM)
    assert prompt.shape is MenuShape.SINGLE_CHOICE
    assert [(o.number, o.label) for o in prompt.options] == [
        (1, "Approve Once — proceed this time only"),
        (2, "Always Approve — proceed and silence this prompt permanently"),
        (3, "Cancel — keep current conversation"),
    ]
    assert answer_digits(prompt) == ("1", "2", "3")


def test_an_or_after_the_comma_still_offers_both_arrows() -> None:
    # The footer reads "type 1/2/3, or ↑/↓ to select": the "or" joins a list, it is not a key.
    hints = BlockedPrompt.parse(SLASH_CONFIRM).hints
    assert [(h.key, h.verb) for h in hints] == [
        ("up", "select"),
        ("down", "select"),
        ("enter", "confirm"),
    ]


def test_the_token_is_the_question_posed_and_the_countdown_is_not_in_it() -> None:
    drawn = BlockedPrompt.parse(SLASH_CONFIRM)
    assert drawn.identity
    assert control_token(drawn) == drawn.identity
    assert control_token(BlockedPrompt.parse(_ticked(SLASH_CONFIRM))) == drawn.identity


def test_a_digit_drawn_before_the_countdown_ticked_is_still_taken(tmp_path: Path) -> None:
    herd = FakeHerd()
    herd.put(P3, S1, status="blocked")
    herd.screens[P3] = [_ticked(SLASH_CONFIRM)]
    drawn = control_token(BlockedPrompt.parse(SLASH_CONFIRM))
    result = send_key(rig(tmp_path, herd).writer, KeyAsk(S1, "3", drawn_from=drawn))
    assert isinstance(result, KeySent), result
    assert herd.writes(SEND_KEYS) == [{"target": "w1:p3", "keys": ["3"]}]


def test_a_pad_key_drawn_before_the_countdown_ticked_is_still_taken(tmp_path: Path) -> None:
    herd = FakeHerd()
    herd.put(P3, S1, status="blocked")
    herd.screens[P3] = [_ticked(SLASH_CONFIRM)]
    drawn = control_token(BlockedPrompt.parse(SLASH_CONFIRM))
    result = send_key(rig(tmp_path, herd).writer, KeyAsk(S1, "enter", drawn_from=drawn))
    assert isinstance(result, KeySent), result
    assert herd.writes(SEND_KEYS) == [{"target": "w1:p3", "keys": ["enter"]}]


def test_a_pad_key_drawn_under_another_screen_is_still_refused(tmp_path: Path) -> None:
    herd = FakeHerd()
    herd.put(P3, S1, status="blocked")
    herd.screens[P3] = [SLASH_CONFIRM.replace("/new", "/reset")]
    drawn = control_token(BlockedPrompt.parse(SLASH_CONFIRM))
    result = send_key(rig(tmp_path, herd).writer, KeyAsk(S1, "enter", drawn_from=drawn))
    assert isinstance(result, Refused), result
    assert result.refusal is Refusal.SCREEN_CHANGED


# herdr lags the dialog closing: measured live on hermes's /new confirm, the dialog left the
# screen within 40ms of the key while herdr said blocked for about 260ms more, then done.
AFTER_ANSWER = (FIXTURES / "hermes" / "answered-still-flagged.txt").read_text()


def _answer_lands(herd: FakeHerd, _params: dict[str, JsonValue]) -> None:
    herd.screens[P3] = [AFTER_ANSWER]

    def herdr_catches_up(herd: FakeHerd, _params: dict[str, JsonValue]) -> None:
        herd.agents[P3]["agent_status"] = "done"

    def still_blocked(herd: FakeHerd, _params: dict[str, JsonValue]) -> None:
        # The fake answers from live state, so the flip lands on the read after this one.
        herd.after["agent.get"] = herdr_catches_up

    herd.after["agent.get"] = still_blocked


def _slash_confirm_rig(tmp_path: Path) -> Rig:
    herd = FakeHerd()
    herd.put(P3, S1, status="blocked")
    herd.screens[P3] = [SLASH_CONFIRM]
    return rig(tmp_path, herd)


def test_the_screen_herdr_still_calls_blocked_holds_no_question() -> None:
    assert not BlockedPrompt.parse(AFTER_ANSWER).identity


def test_a_tap_that_landed_while_herdr_lagged_is_confirmed(tmp_path: Path) -> None:
    r = _slash_confirm_rig(tmp_path)
    r.herd.after[SEND_KEYS] = _answer_lands
    drawn = control_token(BlockedPrompt.parse(SLASH_CONFIRM))
    result = send_key(r.writer, KeyAsk(S1, "3", drawn_from=drawn))
    assert isinstance(result, KeySent), result
    assert (result.outcome, result.evidence) == (Outcome.CONFIRMED, Evidence.GONE)
    assert result.prompt is None
    outcomes = [rec.get("result") for rec in r.audit_records if rec["phase"] == "outcome"]
    assert outcomes == ["confirmed:gone"]


def test_a_dialog_back_after_a_blank_blocked_read_is_not_confirmed(tmp_path: Path) -> None:
    r = _slash_confirm_rig(tmp_path)

    def redrawn(herd: FakeHerd, _params: dict[str, JsonValue]) -> None:
        # One read with the dialog mid-redraw, then the same question again: nothing landed.
        herd.screens[P3] = [AFTER_ANSWER, SLASH_CONFIRM]

    r.herd.after[SEND_KEYS] = redrawn
    drawn = control_token(BlockedPrompt.parse(SLASH_CONFIRM))
    result = send_key(r.writer, KeyAsk(S1, "3", drawn_from=drawn))
    assert isinstance(result, KeySent), result
    assert (result.outcome, result.evidence) == (Outcome.UNCONFIRMED, Evidence.UNMOVED)
    assert result.prompt is not None
    assert control_token(result.prompt) == drawn
