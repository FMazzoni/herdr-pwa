"""Typing a prompt: resolved now, refused at a question, read back, never resent."""

import threading
from pathlib import Path

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st
from pydantic import JsonValue

from herdr_pwa.audit import AUDIT_FILENAME, AuditUnavailableError, Entry, Write
from herdr_pwa.engine import EngineUnavailable, as_session_id, screen_digest
from herdr_pwa.panetext import as_text_read
from herdr_pwa.writes.guard import (
    MAX_PROMPT_CHARS,
    asks_a_question,
    landed,
)
from herdr_pwa.writes.outcome import Evidence, Outcome
from herdr_pwa.writes.prompt import (
    FirstPromptAsk,
    PromptAsk,
    PromptSent,
    send_first_prompt,
    send_prompt,
)
from herdr_pwa.writes.refusal import Refusal, Refused
from herdr_pwa.writes.resolve import InFlight, Writer
from tests._fake_herd import (
    P3,
    P5,
    S1,
    S2,
    SETTLED,
    FakeHerd,
    Rig,
    capture,
    drafted,
    echoed,
    rig,
)

PROMPT = "agent.prompt"


def idle_rig(tmp_path: Path, *screens: str, status: str = "idle") -> Rig:
    herd = FakeHerd()
    herd.put(P3, S1, status=status)
    herd.screens[P3] = list(screens) or [SETTLED]
    return rig(tmp_path, herd)


def sent(result: PromptSent | Refused) -> PromptSent:
    assert isinstance(result, PromptSent), result
    return result


def refused(result: object) -> Refused:
    assert isinstance(result, Refused), result
    return result


def test_a_prompt_at_a_settled_pane_is_typed_once_and_read_back(tmp_path: Path) -> None:
    r = idle_rig(tmp_path, SETTLED, echoed("\u276f hello there"))
    result = sent(send_prompt(r.writer, PromptAsk(S1, "hello there")))
    assert result.verified
    assert result.target == P3
    [typed] = r.herd.writes(PROMPT)
    assert typed == {"target": "w1:p3", "text": "hello there"}
    phases = [(rec["phase"], rec.get("result")) for rec in r.audit_records]
    assert phases == [("attempt", None), ("outcome", "confirmed:echo")]


def test_an_ack_that_cannot_be_read_back_is_sent_unverified_and_never_resent(
    tmp_path: Path,
) -> None:
    r = idle_rig(tmp_path, SETTLED)
    result = sent(send_prompt(r.writer, PromptAsk(S1, "hello there")))
    assert (result.outcome, result.evidence) == (Outcome.ACCEPTED, Evidence.NOECHO)
    assert not result.verified
    assert len(r.herd.writes(PROMPT)) == 1


@pytest.mark.parametrize(
    "fixture",
    [
        "capture-blocked-permission-prompt",
        "capture-blocked-askuserquestion-menu",
        "capture-blocked-multiselect-checklist",
        "capture-blocked-plan-approval",
        "capture-blocked-trust-folder",
    ],
)
def test_a_prompt_is_never_typed_at_a_screen_asking_a_question(
    tmp_path: Path, fixture: str
) -> None:
    r = idle_rig(tmp_path, capture(fixture, "visible"))
    result = refused(send_prompt(r.writer, PromptAsk(S1, "no, do not do that")))
    assert result.refusal is Refusal.QUESTION_SHOWING
    assert result.status == 409
    assert r.herd.writes(PROMPT) == []
    assert r.audit_records == [], "a refusal the app makes on its own is not recorded"


@pytest.mark.parametrize(
    "screen",
    [
        "Which seasons?\n[ ] spring\n[x] summer\n[ ] autumn\n",
        "Pick one?\n  1. Red\n  2. Blue\n",
    ],
)
def test_a_menu_with_no_footer_or_cursor_is_still_a_question(screen: str) -> None:
    assert asks_a_question(screen)


@pytest.mark.parametrize(
    "fixture",
    ["capture-idle-claude-prompt", "capture-idle-numbered-recap", "capture-working-claude"],
)
def test_settled_and_working_panes_are_not_questions(fixture: str) -> None:
    assert not asks_a_question(capture(fixture, "visible"))


def test_a_pane_that_cannot_be_read_refuses_rather_than_sending_blind(tmp_path: Path) -> None:
    r = idle_rig(tmp_path)
    r.herd.fail["agent.read"] = EngineUnavailable("down")
    result = refused(send_prompt(r.writer, PromptAsk(S1, "hello")))
    assert (result.refusal, result.status) == (Refusal.UNREADABLE, 503)
    assert r.herd.writes(PROMPT) == []


def test_a_herd_that_cannot_be_read_refuses_with_503(tmp_path: Path) -> None:
    r = idle_rig(tmp_path)
    r.herd.fail["session.snapshot"] = EngineUnavailable("down")
    result = refused(send_prompt(r.writer, PromptAsk(S1, "hello")))
    assert (result.refusal, result.status) == (Refusal.UNREACHABLE, 503)
    assert r.herd.writes(PROMPT) == []


def test_a_gone_session_is_404_before_the_pane_is_read(tmp_path: Path) -> None:
    r = idle_rig(tmp_path)
    result = refused(send_prompt(r.writer, PromptAsk(S2, "hello")))
    assert (result.refusal, result.status) == (Refusal.GONE, 404)
    assert r.herd.writes("agent.read") == []


def test_an_empty_session_matches_no_agent(tmp_path: Path) -> None:
    r = idle_rig(tmp_path)
    result = refused(send_prompt(r.writer, PromptAsk(as_session_id(""), "hello")))
    assert result.refusal is Refusal.GONE


def test_a_pane_id_posing_as_a_session_raises_rather_than_reading_as_gone() -> None:
    with pytest.raises(ValueError, match="pane id"):
        _ = as_session_id("w1:p3")


def test_a_moved_session_is_followed_to_where_it_is_now(tmp_path: Path) -> None:
    herd = FakeHerd()
    herd.put(P5, S1)
    herd.put(P3, S2)
    herd.screens[P5] = [SETTLED]
    r = rig(tmp_path, herd)
    result = sent(send_prompt(r.writer, PromptAsk(S1, "hello there", expected_target=P3)))
    assert result.target == P5
    assert [w["target"] for w in herd.writes(PROMPT)] == ["w1:p5"]


def test_a_recycled_pane_is_caught_on_the_wire_and_nothing_is_typed(tmp_path: Path) -> None:
    """The misroute: the snapshot still says S1 is at p3, the pane itself is held by S2."""
    r = idle_rig(tmp_path)
    impostor = dict(r.herd.agents[P3])
    impostor["agent_session"] = {"agent": "claude", "kind": "id", "source": "x", "value": S2}
    r.herd.holders[P3] = impostor
    result = refused(send_prompt(r.writer, PromptAsk(S1, "hello")))
    assert (result.refusal, result.status) == (Refusal.SESSION_MOVED, 409)
    assert r.herd.writes(PROMPT) == []
    outcome = r.audit_records[-1]
    assert outcome["phase"] == "outcome"
    assert outcome["result"] == "refused:TargetNotFound"


def test_a_screen_digest_that_no_longer_matches_refuses_and_reissues(tmp_path: Path) -> None:
    r = idle_rig(tmp_path)
    result = refused(send_prompt(r.writer, PromptAsk(S1, "hello", drawn_against="stale")))
    assert result.refusal is Refusal.SCREEN_CHANGED
    assert result.screen == screen_digest(as_text_read(SETTLED))
    retry = send_prompt(r.writer, PromptAsk(S1, "hello", drawn_against=result.screen))
    assert isinstance(retry, PromptSent)


def test_a_working_agent_is_issued_no_digest_to_carry(tmp_path: Path) -> None:
    r = idle_rig(tmp_path, status="working")
    result = refused(send_prompt(r.writer, PromptAsk(S1, "hello", drawn_against="stale")))
    assert result.refusal is Refusal.SCREEN_CHANGED
    assert result.screen == ""


def test_a_shell_command_in_a_prompt_goes_on_the_first_tap(tmp_path: Path) -> None:
    # What an agent runs is left to the agent's own permissions, not second-guessed here.
    r = idle_rig(tmp_path)
    _ = sent(send_prompt(r.writer, PromptAsk(S1, "rm -rf ./node_modules && git push --force")))
    assert len(r.herd.writes(PROMPT)) == 1


class _Unrecordable:
    def record(self, write: Write, /) -> Entry:
        raise AuditUnavailableError(f"disk full for {write.endpoint}")


def test_a_write_that_cannot_be_recorded_is_not_made(tmp_path: Path) -> None:
    r = idle_rig(tmp_path)
    writer = Writer(r.writer.client, _Unrecordable(), r.writer.pacing, memory=r.writer.memory)
    result = refused(send_prompt(writer, PromptAsk(S1, "hello")))
    assert (result.refusal, result.status) == (Refusal.UNRECORDABLE, 503)
    assert r.herd.writes(PROMPT) == []


def test_protocol_drift_fails_closed(tmp_path: Path) -> None:
    r = idle_rig(tmp_path)
    r.herd.protocol = 23
    result = refused(send_prompt(r.writer, PromptAsk(S1, "hello")))
    assert (result.refusal, result.status) == (Refusal.PROTOCOL_DRIFT, 502)
    assert r.herd.writes(PROMPT) == []


@pytest.mark.parametrize(
    ("text", "refusal"), [("   ", Refusal.EMPTY), ("x" * (MAX_PROMPT_CHARS + 1), Refusal.TOO_LONG)]
)
def test_unsendable_text_is_refused(tmp_path: Path, text: str, refusal: Refusal) -> None:
    r = idle_rig(tmp_path)
    assert refused(send_prompt(r.writer, PromptAsk(S1, text))).refusal is refusal


def test_a_first_prompt_waits_for_a_settled_screen_and_asserts_no_session(
    tmp_path: Path,
) -> None:
    herd = FakeHerd()
    herd.put(P3, "", agent="hermes", interactive_ready=True, launch_pending=False)
    herd.screens[P3] = [
        "drawing",
        SETTLED,
        SETTLED,
        SETTLED,
        SETTLED,
        echoed("\u276f hi hermes"),
    ]
    r = rig(tmp_path, herd)
    result = sent(send_first_prompt(r.writer, FirstPromptAsk(P3, "hi hermes")))
    assert result.verified
    assert herd.writes(PROMPT) == [{"target": "w1:p3", "text": "hi hermes"}]


def test_a_first_prompt_at_an_agent_with_a_session_is_sent_to_its_own_page(
    tmp_path: Path,
) -> None:
    r = idle_rig(tmp_path)
    assert (
        refused(send_first_prompt(r.writer, FirstPromptAsk(P3, "hi"))).refusal
        is Refusal.HAS_IDENTITY
    )


def test_text_left_in_the_input_box_has_not_landed() -> None:
    assert not landed("hello there", SETTLED, drafted("hello there"))
    assert landed("hello there", SETTLED, echoed("\u276f hello there"))


def test_landing_needs_more_copies_than_before() -> None:
    assert landed("hello world", "", "\u276f hello world")
    assert not landed("hello world", "\u276f hello world", "\u276f hello world")


# What a terminal draws under the line its cursor waits on.
_STATUS_BAR = '[hp] 0:bash*  "host" 12:00 21-Oct-26'
_FOOTER = "  esc to interrupt \u00b7 ctrl+c to exit"


@pytest.mark.parametrize(
    "screen",
    [
        pytest.param("[sudo] password for owner:", id="alone"),
        pytest.param(f"[sudo] password for owner:\n{_STATUS_BAR}", id="status-line"),
        pytest.param(f"Password:\n\n{_STATUS_BAR}\n0:zsh 1:vim\nrepo main*>", id="four-under"),
    ],
)
def test_at_a_hidden_input_prompt_the_bytes_are_the_same_and_the_text_is_not_stored(
    tmp_path: Path, screen: str
) -> None:
    r = idle_rig(tmp_path, screen)
    result = sent(send_prompt(r.writer, PromptAsk(S1, "hunter2-secret")))
    assert (result.outcome, result.evidence) == (Outcome.ACCEPTED, Evidence.NOECHO)
    assert r.herd.writes(PROMPT)[0]["text"] == "hunter2-secret"
    assert "hunter2-secret" not in _log_text(r)


def _log_text(r: Rig) -> str:
    log = r.state / AUDIT_FILENAME
    return log.read_text() if log.exists() else ""


def test_an_echoed_prompt_keeps_its_text_on_the_outcome_alone(tmp_path: Path) -> None:
    r = idle_rig(tmp_path, SETTLED, echoed("\u276f hello there"))
    assert sent(send_prompt(r.writer, PromptAsk(S1, "hello there"))).verified
    attempt, outcome = r.audit_records
    assert (attempt.get("payload"), attempt.get("payload_chars")) == (None, 11)
    assert (outcome.get("result"), outcome.get("payload")) == ("confirmed:echo", "hello there")


def test_an_unechoed_prompt_keeps_only_its_length(tmp_path: Path) -> None:
    r = idle_rig(tmp_path, SETTLED)
    result = sent(send_prompt(r.writer, PromptAsk(S1, "hunter2-secret")))
    assert (result.outcome, result.evidence) == (Outcome.ACCEPTED, Evidence.NOECHO)
    assert "hunter2-secret" not in _log_text(r)
    assert r.audit_records[0].get("payload_chars") == 14


def test_a_failed_prompt_keeps_only_its_length(tmp_path: Path) -> None:
    r = idle_rig(tmp_path)
    r.herd.fail["agent.prompt"] = EngineUnavailable("down")
    _ = refused(send_prompt(r.writer, PromptAsk(S1, "hunter2-secret")))
    assert "hunter2-secret" not in _log_text(r)
    assert [rec["phase"] for rec in r.audit_records] == ["attempt", "outcome"]


def test_a_refused_prompt_records_nothing(tmp_path: Path) -> None:
    r = idle_rig(tmp_path, capture("capture-blocked-permission-prompt"))
    _ = refused(send_prompt(r.writer, PromptAsk(S1, "hunter2-secret")))
    assert "hunter2-secret" not in _log_text(r)


_LINE = st.text(st.characters(codec="utf-8", exclude_categories=("Cs", "Cc")), max_size=40)
_SCREEN = st.lists(
    st.one_of(
        _LINE,
        st.sampled_from(
            [
                "[sudo] password for owner:",
                "Password:",
                "Enter passphrase:",
                _STATUS_BAR,
                _FOOTER,
                "\u2502 \u2502",
                "repo main*>",
                "\u276f ",
                "",
            ]
        ),
    ),
    max_size=10,
).map("\n".join)
_SECRET = st.from_regex(r"[A-Za-z0-9!@#%^&*_+=-]{8,32}", fullmatch=True)


@settings(
    max_examples=80, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(screens=st.tuples(_SCREEN, _SCREEN, st.booleans()), secret=_SECRET)
def test_no_audit_file_holds_text_whose_send_was_not_echoed(
    tmp_path_factory: pytest.TempPathFactory, screens: tuple[str, str, bool], secret: str
) -> None:
    # Whatever the screen shows, a miss by the detector can no longer leak the text.
    before, after, echo = screens
    later = f"{after}\n{secret}" if echo else after
    r = idle_rig(tmp_path_factory.mktemp("audit"), before, later)
    result = send_prompt(r.writer, PromptAsk(S1, secret))
    if not (isinstance(result, PromptSent) and result.verified):
        assert secret not in _log_text(r)


def _kept(r: Rig) -> list[object]:
    return [rec.get("payload") for rec in r.audit_records if rec.get("payload") is not None]


_PINENTRY = (
    "\u250c\u2500\u2500 pinentry \u2500\u2500\u2510\n"
    "\u2502 Please enter the passphrase to unlock the key \u2502\n"
    "\u2502 Passphrase: ________ \u2502\n"
    "\u2514\u2500\u2500\u2500\u2500\u2500\u2500\u2518"
)


@pytest.mark.parametrize(
    ("before", "after", "secret"),
    [
        pytest.param(
            "[sudo] password for owner:",
            "[sudo] password for owner:\nSorry, try again.\n[sudo] password for owner:",
            "password1!",
            id="sudo-retry",
        ),
        pytest.param(
            "owner@host's password:",
            "owner@host's password:\nPermission denied, please try again.\nowner@host's password:",
            "owner@ho-secret",
            id="ssh-retry",
        ),
        pytest.param(
            "Enter PIN:", "Enter PIN:\nbuilt 2026-10-21 in 2026 ms", "2026", id="short-pin"
        ),
        pytest.param(
            SETTLED, echoed("\u276f hunter2- was here"), "hunter2-REALSECRET", id="prefix"
        ),
        pytest.param(
            _PINENTRY,
            "$ echo s3cret-passphrase-xyz\ns3cret-passphrase-xyz\nowner@host:~$",
            "s3cret-passphrase-xyz",
            id="pinentry-return",
        ),
    ],
)
def test_text_that_did_not_show_whole_and_new_is_never_kept(
    tmp_path: Path, before: str, after: str, secret: str
) -> None:
    r = idle_rig(tmp_path, before, after)
    _ = send_prompt(r.writer, PromptAsk(S1, secret))
    assert _kept(r) == []
    # A short secret can match a timestamp, so only a long one is sought in the raw file.
    assert len(secret) < 8 or secret not in _log_text(r)


def _flat(text: str) -> str:
    return "".join(text.split())


def _reprompt(asks: str, tail: str) -> tuple[str, str, str]:
    # The secret shares the prompt's first eight characters, which the re-prompt draws again.
    return asks, f"{asks}\nSorry, try again.\n{asks}", _flat(asks)[:8] + tail


def _short(pin: str) -> tuple[str, str, str]:
    return "Enter PIN:", f"Enter PIN:\nbuilt {pin} files in {pin} ms", pin


def _prefix(head: str, tail: str) -> tuple[str, str, str]:
    return "Password:", f"Password:\n{head} shown alone", head + tail


_ASKS = st.sampled_from(["[sudo] password for owner:", "owner@host's password:", "Enter PIN:"])
_LEAKY = st.one_of(
    st.builds(_reprompt, _ASKS, _SECRET),
    st.builds(_short, st.from_regex(r"[0-9A-Za-z]{1,7}", fullmatch=True)),
    st.builds(_prefix, st.from_regex(r"[A-Za-z0-9]{8}", fullmatch=True), _SECRET),
)


@settings(
    max_examples=80, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(case=_LEAKY)
def test_no_audit_file_keeps_text_that_did_not_show_whole_and_new(
    tmp_path_factory: pytest.TempPathFactory, case: tuple[str, str, str]
) -> None:
    before, after, secret = case
    needle = _flat(secret)
    assume(len(needle) < 8 or _flat(after).count(needle) <= _flat(before).count(needle))
    r = idle_rig(tmp_path_factory.mktemp("audit"), before, after)
    _ = send_prompt(r.writer, PromptAsk(S1, secret))
    assert _kept(r) == []


def test_a_prompt_from_a_second_tab_mid_send_is_refused_not_typed_twice(tmp_path: Path) -> None:
    r = idle_rig(tmp_path, SETTLED, echoed("\u276f hello there"))
    second: list[PromptSent | Refused] = []

    def other_tab(herd: FakeHerd, params: dict[str, JsonValue]) -> None:
        del herd.after["agent.read"], params
        second.append(send_prompt(r.writer, PromptAsk(S1, "hello there")))

    r.herd.after["agent.read"] = other_tab
    _ = sent(send_prompt(r.writer, PromptAsk(S1, "hello there")))
    [late] = second
    assert refused(late).refusal is Refusal.IN_FLIGHT
    assert len(r.herd.writes(PROMPT)) == 1


def test_in_flight_is_held_across_threads_until_the_write_ends() -> None:
    flights = InFlight()
    inside, release = threading.Event(), threading.Event()
    seen: list[bool] = []

    def first() -> None:
        with flights.hold("session:a") as mine:
            seen.append(mine)
            inside.set()
            assert release.wait(5)

    worker = threading.Thread(target=first)
    worker.start()
    assert inside.wait(5)
    with flights.hold("session:a") as mine, flights.hold("session:b") as other:
        assert (mine, other) == (False, True)
    release.set()
    worker.join()
    with flights.hold("session:a") as mine:
        assert mine
    assert seen == [True]


def _lost(_herd: FakeHerd, _params: dict[str, JsonValue]) -> None:
    lost = EngineUnavailable("the runner closed the connection while answering")
    lost.after_send = True
    raise lost


def test_a_lost_answer_hands_back_a_fresh_digest_and_holds_the_same_text(tmp_path: Path) -> None:
    r = idle_rig(tmp_path)
    r.herd.after[PROMPT] = _lost
    lost = refused(send_prompt(r.writer, PromptAsk(S1, "hello there")))
    assert (lost.refusal, lost.screen) == (
        Refusal.ANSWER_LOST,
        screen_digest(as_text_read(SETTLED)),
    )
    assert lost.asks_again
    del r.herd.after[PROMPT]
    held = refused(send_prompt(r.writer, PromptAsk(S1, "hello there", drawn_against=lost.screen)))
    assert (held.refusal, held.status, held.screen) == (Refusal.RESEND, 200, lost.screen)
    # Different text is a different prompt, and goes.
    _ = sent(send_prompt(r.writer, PromptAsk(S1, "something else")))
    assert len(r.herd.writes(PROMPT)) == 2
