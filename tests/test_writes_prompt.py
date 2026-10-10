"""Typing a prompt: resolved now, refused at a question, read back, never resent."""

from pathlib import Path

import pytest

from herdr_pwa.audit import AuditUnavailableError, Entry, Write
from herdr_pwa.engine.model import as_session_id, screen_digest
from herdr_pwa.engine.transport import EngineUnavailable
from herdr_pwa.writes.guard import (
    MAX_PROMPT_CHARS,
    NO_ECHO_MECHANISM,
    asks_a_question,
    destructive_reason,
    landed,
    no_echo_notice,
)
from herdr_pwa.writes.outcome import Evidence, Outcome
from herdr_pwa.writes.prompt import PromptAsk, PromptSent, send_first_prompt, send_prompt
from herdr_pwa.writes.refusal import Refusal, Refused
from herdr_pwa.writes.resolve import Writer
from tests._fake_herd import P3, P5, S1, S2, SETTLED, FakeHerd, Rig, capture, rig

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
    r = idle_rig(tmp_path, SETTLED, SETTLED + "\u276f hello there\n")
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
    assert outcome["result"] == "refused:TargetNotFound"


def test_a_screen_digest_that_no_longer_matches_refuses_and_reissues(tmp_path: Path) -> None:
    r = idle_rig(tmp_path)
    result = refused(send_prompt(r.writer, PromptAsk(S1, "hello", drawn_against="stale")))
    assert result.refusal is Refusal.SCREEN_CHANGED
    assert result.screen == screen_digest(SETTLED)
    retry = send_prompt(r.writer, PromptAsk(S1, "hello", drawn_against=result.screen))
    assert isinstance(retry, PromptSent)


def test_a_working_agent_is_issued_no_digest_to_carry(tmp_path: Path) -> None:
    r = idle_rig(tmp_path, status="working")
    result = refused(send_prompt(r.writer, PromptAsk(S1, "rm -rf build")))
    assert result.refusal is Refusal.CONFIRM
    assert result.screen == ""


def test_a_destructive_prompt_is_held_then_sent_on_the_second_tap(tmp_path: Path) -> None:
    r = idle_rig(tmp_path)
    held = refused(send_prompt(r.writer, PromptAsk(S1, "rm -rf ./node_modules")))
    assert held.held
    assert held.status == 200
    assert "rm with a recursive flag" in held.reason
    assert r.herd.writes(PROMPT) == []
    again = PromptAsk(S1, "rm -rf ./node_modules", drawn_against=held.screen, confirmed=True)
    _ = sent(send_prompt(r.writer, again))
    assert len(r.herd.writes(PROMPT)) == 1


class _Unrecordable:
    def record(self, write: Write, /) -> Entry:
        raise AuditUnavailableError(f"disk full for {write.endpoint}")


def test_a_write_that_cannot_be_recorded_is_not_made(tmp_path: Path) -> None:
    r = idle_rig(tmp_path)
    writer = Writer(r.writer.client, _Unrecordable(), r.writer.pacing)
    result = refused(send_prompt(writer, PromptAsk(S1, "hello")))
    assert (result.refusal, result.status) == (Refusal.UNRECORDABLE, 503)
    assert r.herd.writes(PROMPT) == []


def test_at_a_no_echo_prompt_the_payload_is_not_stored_and_the_bytes_are_the_same(
    tmp_path: Path,
) -> None:
    r = idle_rig(tmp_path, "[sudo] password for owner:")
    result = sent(send_prompt(r.writer, PromptAsk(S1, "hunter2-secret")))
    assert result.no_echo == NO_ECHO_MECHANISM
    assert r.herd.writes(PROMPT)[0]["text"] == "hunter2-secret"
    attempt = r.audit_records[0]
    assert attempt["payload"] is None
    assert attempt["payload_omitted"] == "no_echo_prompt"


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
        SETTLED + "\u276f hi hermes\n",
    ]
    r = rig(tmp_path, herd)
    result = sent(send_first_prompt(r.writer, P3, "hi hermes"))
    assert result.verified
    assert herd.writes(PROMPT) == [{"target": "w1:p3", "text": "hi hermes"}]


def test_a_first_prompt_at_an_agent_with_a_session_is_sent_to_its_own_page(
    tmp_path: Path,
) -> None:
    r = idle_rig(tmp_path)
    assert refused(send_first_prompt(r.writer, P3, "hi")).refusal is Refusal.HAS_IDENTITY


# The guard's own tables, lifted with their reasons.


@pytest.mark.parametrize(
    ("text", "named"),
    [
        ("rm -rf build", "rm with a recursive flag"),
        ("rm --recursive ./out", "rm with a recursive flag"),
        ("git push --force origin main", "git push --force"),
        ("git push -f", "git push --force"),
        ("sudo systemctl restart nginx", "sudo"),
        ("dd if=/dev/zero of=/dev/sda", "dd with if="),
        ("mkfs.ext4 /dev/sdb1", "mkfs"),
        ("chmod -R 777 /srv", "chmod with a recursive flag"),
        ("echo broken > /etc/hosts", "a redirect onto a system path"),
        ("npm publish --force", "--force"),
        ("$ sudo rm -rf /", "sudo"),
    ],
)
def test_the_verbs_worth_a_second_tap(text: str, named: str) -> None:
    assert named in destructive_reason(text)


@pytest.mark.parametrize(
    "text",
    [
        "let us play sudoku after this",
        "rm foo.txt; ls -r",
        "cat notes.md | grep -R todo",
        "the rm -rf in the Makefile is wrong, please fix it",
        "explain why git push --force is dangerous",
        "",
    ],
)
def test_prose_about_those_verbs_does_not_ask(text: str) -> None:
    assert destructive_reason(text) == ""


def test_landing_needs_more_copies_than_before() -> None:
    assert landed("hello world", "", "\u276f hello world")
    assert not landed("hello world", "\u276f hello world", "\u276f hello world")


@pytest.mark.parametrize(
    "tail", ["[sudo] password for owner:", "Enter passphrase for /k:", "Retype password:"]
)
def test_no_echo_prompts_are_recognised(tail: str) -> None:
    assert no_echo_notice("earlier\n" + tail) == NO_ECHO_MECHANISM


@pytest.mark.parametrize("screen", ["Password:", "\u276f [sudo] password for owner:", SETTLED, ""])
def test_anything_unsure_is_not_called_no_echo(screen: str) -> None:
    assert no_echo_notice(screen) == ""
