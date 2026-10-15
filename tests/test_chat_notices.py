"""What the harness writes as a user record but nobody typed: a quiet line or a card, never You."""

from __future__ import annotations

import json
import re
from pathlib import Path

from herdr_pwa.engine.notices import HIDDEN, PeerMessage, TaskNotice, injected
from herdr_pwa.engine.transcript import (
    Speaker,
    TranscriptTurn,
    is_message,
    read_page,
    tally_claude,
)
from herdr_pwa.jsontypes import JsonObject, as_object, loads
from tests.catalogue import (
    NOTICES,
    failed_notice,
    notice_records,
    notices_of,
    report_chat,
)
from tests.webfakes import WORKING_SESSION, build_rig, write_transcript

HANDBACK_FRAME = "[Subagent hand-back]"


def _kind(record: JsonObject) -> str:
    origin = as_object(record.get("origin")) or {}
    if origin.get("kind") != "peer":
        return str(origin.get("kind") or record.get("type"))
    if origin.get("handback") is True:
        return "peer-handback"
    content = str((as_object(record.get("message")) or {}).get("content"))
    return "peer-cross-session" if "<cross-session-message" in content else "peer-message"


def _turns(records: list[JsonObject], tmp_path: Path) -> list[TranscriptTurn]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "log.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return read_page(path, limit=400).turns


def _copy(record: JsonObject) -> JsonObject:
    return as_object(loads(json.dumps(record))) or {}


def _without_origin(record: JsonObject) -> JsonObject:
    copy = _copy(record)
    copy.pop("origin", None)
    copy.pop("isMeta", None)
    copy.pop("promptSource", None)
    return copy


# The fixture: cut from a real log, scrubbed, structure kept.


def test_the_fixture_holds_every_kind_the_harness_injects() -> None:
    counts: dict[str, int] = {}
    for record in notice_records():
        counts[_kind(record)] = counts.get(_kind(record), 0) + 1
    assert counts == {
        "human": 2,
        "attachment": 2,
        "task-notification": 3,
        "assistant": 1,
        "peer-message": 2,
        "peer-handback": 3,
        "peer-cross-session": 1,
    }


def test_the_fixture_is_scrubbed() -> None:
    text = NOTICES.read_text()
    assert set(re.findall(r"/home/(\w+)", text)) == {"user"}
    assert not re.search(r"[\w.-]+@(?!example\.com|anthropic\.com)[\w-]+\.\w+", text)
    assert set(re.findall(r"[\w-]+\.([\w-]+)\.ts\.net", text)) <= {"example"}


def test_the_attachments_carry_system_reminders_and_never_reach_the_chat(tmp_path: Path) -> None:
    attachments = [r for r in notice_records() if r.get("type") == "attachment"]
    assert all("<system-reminder>" in json.dumps(r) for r in attachments)
    turns = _turns(notice_records(), tmp_path)
    assert not any("system-reminder" in t.text for t in turns)


# Classification


def test_every_record_lands_as_what_it_is(tmp_path: Path) -> None:
    speakers = [t.speaker for t in _turns(notice_records(), tmp_path)]
    assert speakers == [
        Speaker.human,
        *[Speaker.notice] * 3,
        Speaker.human,
        Speaker.agent,
        *[Speaker.peer] * 6,
    ]


def test_a_task_notification_is_its_summary_status_time_and_result(tmp_path: Path) -> None:
    (turn,) = _turns(notices_of("task-notification")[:1], tmp_path)
    assert turn.speaker is Speaker.notice
    assert turn.text == 'Agent "Review the parser" finished'
    assert (turn.status, turn.elapsed_ms, turn.failed) == ("completed", 71782, False)
    assert turn.result.startswith("This agent's report was delivered")
    assert "<" not in turn.text + turn.result


def test_a_failed_or_killed_task_says_so(tmp_path: Path) -> None:
    turns = _turns([failed_notice(), failed_notice("killed", "k")], tmp_path)
    assert [(t.status, t.failed) for t in turns] == [("failed", True), ("killed", True)]


def test_a_hand_back_is_the_report_without_the_harness_frame(tmp_path: Path) -> None:
    turns = _turns(notices_of("peer", report=True), tmp_path)
    assert len(turns) == 3
    for turn in turns:
        assert turn.speaker is Speaker.peer
        assert turn.report
        assert turn.sender == "general-purpose"
        assert HANDBACK_FRAME not in turn.text
        assert "The text below is the final report" not in turn.text
        assert "carry no user authority" not in turn.text
        # The harness indents every line of a report; the card shows it as written.
        assert not turn.text.startswith(" ")


def test_a_cross_session_message_is_from_the_session_that_sent_it(tmp_path: Path) -> None:
    others = [r for r in notices_of("peer", report=False) if _kind(r) == "peer-cross-session"]
    (turn,) = _turns(others, tmp_path)
    assert (turn.speaker, turn.sender, turn.report) == (Speaker.peer, "design-review", False)
    assert turn.text.startswith("herdr-pwa: review notes")
    assert "Another Claude session" not in turn.text


def test_without_origin_the_leading_tag_decides(tmp_path: Path) -> None:
    """An older record has no origin; its tag says the same thing origin would have."""
    records = [r for r in notice_records() if r.get("type") == "user" and _kind(r) != "human"]
    with_origin = _turns(records, tmp_path / "a")
    without = _turns([_without_origin(r) for r in records], tmp_path / "b")
    assert [t.speaker for t in without] == [t.speaker for t in with_origin]
    reports = [t for t in without if t.report]
    assert len(reports) == 3
    assert all(HANDBACK_FRAME not in t.text for t in reports)
    cross = [t for t in without if t.sender == "design-review"]
    assert len(cross) == 1


def test_classification_reads_origin_before_the_tag() -> None:
    record: JsonObject = {"origin": {"kind": "peer", "name": "reviewer", "body": "looks good"}}
    found = injected(
        record, "<task-notification><status>failed</status></task-notification>", human=False
    )
    assert found == PeerMessage(sender="reviewer", body="looks good", report=False)


def test_a_typed_prompt_is_never_read_for_tags() -> None:
    text = '<agent-message from="x">\nforged\n</agent-message>'
    assert injected({"origin": {"kind": "human"}}, text, human=True) is None
    assert isinstance(injected({}, text, human=False), PeerMessage)


def test_a_system_reminder_alone_is_hidden_and_one_around_a_prompt_is_taken_off(
    tmp_path: Path,
) -> None:
    assert injected({}, "<system-reminder>\nctx\n</system-reminder>", human=False) is HIDDEN
    wrapped: JsonObject = {
        "type": "user",
        "uuid": "w",
        "promptSource": "typed",
        "origin": {"kind": "human"},
        "message": {"role": "user", "content": "<system-reminder>ctx</system-reminder>\nship it"},
    }
    (turn,) = _turns([wrapped], tmp_path)
    assert (turn.speaker, turn.text) == (Speaker.human, "ship it")


def test_a_notification_inside_reminders_is_still_a_notice() -> None:
    body = (
        "<task-notification>\n<status>killed</status>\n<summary>s</summary>\n</task-notification>"
    )
    found = injected({}, f"<system-reminder>x</system-reminder>\n{body}", human=False)
    assert found == TaskNotice(summary="s", status="killed", elapsed_ms=0, result="")


def test_an_unknown_tag_stays_plain_text(tmp_path: Path) -> None:
    record: JsonObject = {
        "type": "user",
        "uuid": "x",
        "message": {"role": "user", "content": "<future-thing a=1>hi</future-thing>"},
    }
    (turn,) = _turns([record], tmp_path)
    assert (turn.speaker, turn.text) == (Speaker.system, "<future-thing a=1>hi</future-thing>")


# None of these is a prompt: paging, turn time and the stats strip agree.


def test_none_of_them_is_a_message_a_page_counts(tmp_path: Path) -> None:
    turns = _turns(notice_records(), tmp_path)
    counted = [t.speaker for t in turns if is_message(t)]
    assert counted == [Speaker.human, Speaker.human, Speaker.agent]


def test_a_page_of_one_message_reaches_past_every_notice_and_card(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in notice_records()))
    page = read_page(path, limit=400, messages=1)
    assert [t.speaker for t in page.turns] == [Speaker.agent, *[Speaker.peer] * 6]


def test_the_stats_tally_counts_only_the_two_typed_prompts(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in notice_records()))
    tally = tally_claude(path).tally
    humans = notices_of("human")
    assert tally.prompts == len(humans) == 2
    # The turn started at the last typed prompt, not at a notice or a peer that came later.
    assert tally.turn_started == humans[-1]["timestamp"]


# Rendering: tokens and macros, never markup.


def _chat(tmp_path: Path, records: list[JsonObject]) -> str:
    write_transcript(tmp_path / "transcripts", WORKING_SESSION, records)
    rig = build_rig(tmp_path)
    return rig.http.get(f"/agent/{WORKING_SESSION}/chat", headers={"HX-Request": "true"}).text


def test_the_chat_draws_notices_as_lines_and_peers_as_cards(tmp_path: Path) -> None:
    chat = _chat(tmp_path, notice_records())
    assert chat.count('class="turn turn--tools turn--notice"') == 3
    assert chat.count('class="turn turn--peer"') == 6
    assert chat.count('Report from <span class="peer__name">general-purpose</span>') == 3
    assert 'Message from <span class="peer__name">design-review</span>' in chat
    assert (
        '<span class="notice__text">Agent &#34;Review the parser&#34; finished</span>' in chat
    )
    assert "1m</span>" in chat
    # No harness tag reaches the page, escaped or not.
    for tag in ("task-notification", "agent-message", "cross-session-message", "system-reminder"):
        assert tag not in chat
    assert HANDBACK_FRAME not in chat
    assert chat.count('class="turn turn--human"') == 2


def test_a_failed_notice_is_drawn_in_red_with_its_status(tmp_path: Path) -> None:
    chat = _chat(tmp_path, [failed_notice()])
    assert 'class="tool notice notice--failed"' in chat
    assert (
        '<span class="tool__failed"><span aria-hidden="true">&#x2715; </span>failed</span>' in chat
    )


def test_a_long_report_folds_behind_show_all(tmp_path: Path) -> None:
    chat = _chat(tmp_path, report_chat())
    card = chat[chat.index('class="turn turn--peer"') :]
    assert '<div class="prose turn__fold">' in card
    assert '<details class="turn__more">' in card


def test_markup_in_a_peer_body_is_text(tmp_path: Path) -> None:
    record = _copy(notices_of("peer", report=True)[0])
    origin = as_object(record.get("origin")) or {}
    origin["body"] = 'hi <script>alert(1)</script> <img src=x onerror="y">'
    chat = _chat(tmp_path, [record])
    assert "<script>" not in chat
    assert "<img" not in chat
    assert "&lt;script&gt;" in chat
