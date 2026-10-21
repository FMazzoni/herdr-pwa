"""What the harness writes as a user record but nobody typed: a quiet line or a card, never You."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from pydantic import JsonValue

from herdr_pwa.engine import Speaker, TranscriptTurn
from herdr_pwa.web.chat import joined
from tests._jsonpath import obj
from tests.catalogue import (
    COMMAND_EXIT_0,
    COMMAND_EXIT_1,
    NOTICES,
    REPORTED,
    REPORTED_AFTER,
    REPORTED_BEFORE,
    UNREPORTED,
    failed_command,
    failed_notice,
    notice_record,
    notice_records,
    notices_of,
    origin_of,
    report_chat,
)
from tests.engine_seam import (
    HIDDEN,
    PeerMessage,
    RecordOrigin,
    TaskNotice,
    Window,
    injected,
    is_message,
    read_page,
    tally_claude,
)
from tests.webfakes import WORKING_SESSION, build_rig, write_transcript

HANDBACK_FRAME = "[Subagent hand-back]"


def _kind(record: dict[str, JsonValue]) -> str:
    origin = origin_of(record)
    if origin.get("kind") != "peer":
        return str(origin.get("kind") or record.get("type"))
    if origin.get("handback") is True:
        return "peer-handback"
    content = str(obj(record, "message").get("content"))
    return "peer-cross-session" if "<cross-session-message" in content else "peer-message"


def _turns(records: list[dict[str, JsonValue]], tmp_path: Path) -> list[TranscriptTurn]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "log.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return read_page(path, Window(limit=400)).turns


def _copy(record: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return copy.deepcopy(record)


def _without_origin(record: dict[str, JsonValue]) -> dict[str, JsonValue]:
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
        "task-notification": 7,
        "assistant": 1,
        "peer-message": 2,
        "peer-handback": 8,
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
    # A report later in the log names the tag in prose; the tag itself never arrives.
    assert not any("<system-reminder>" in t.text for t in turns)


# Classification


def test_every_record_lands_as_what_it_is(tmp_path: Path) -> None:
    speakers = [t.speaker for t in _turns(notice_records(), tmp_path)]
    assert speakers == [
        Speaker.human,
        Speaker.peer,
        Speaker.notice,
        Speaker.notice,
        Speaker.peer,
        Speaker.peer,
        Speaker.notice,
        Speaker.human,
        Speaker.agent,
        *[Speaker.peer] * 6,
        *TAIL,
    ]


# The records cut from later in the same log: a notice, a report, two notices, a report, one.
TAIL = (Speaker.notice, Speaker.peer, Speaker.notice, Speaker.notice, Speaker.peer, Speaker.notice)


def test_a_task_notification_is_its_summary_status_time_and_result(tmp_path: Path) -> None:
    (turn,) = _turns(notices_of("task-notification")[:1], tmp_path)
    assert turn.speaker is Speaker.notice
    assert (turn.tool, turn.text) == ("Agent", "Review the parser")
    assert (turn.status, turn.elapsed_ms, turn.failed) == ("finished", 71782, False)
    # The harness's result only says the report went elsewhere; it is never shown.
    assert turn.result == ""


def test_a_failed_or_killed_task_says_so(tmp_path: Path) -> None:
    turns = _turns([failed_notice(), failed_notice("killed", "k")], tmp_path)
    assert [(t.status, t.failed) for t in turns] == [("failed", True), ("killed", True)]


def test_a_hand_back_is_the_report_without_the_harness_frame(tmp_path: Path) -> None:
    turns = _turns(notices_of("peer", report=True), tmp_path)
    assert len(turns) == 8
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
    assert len(reports) == 8
    assert all(HANDBACK_FRAME not in t.text for t in reports)
    cross = [t for t in without if t.sender == "design-review"]
    assert len(cross) == 1


def test_classification_reads_origin_before_the_tag() -> None:
    origin = RecordOrigin(kind="peer", name="reviewer", body="looks good")
    found = injected(
        origin, "<task-notification><status>failed</status></task-notification>", human=False
    )
    assert found == PeerMessage(sender="reviewer", body="looks good", report=False)


def test_a_typed_prompt_is_never_read_for_tags() -> None:
    text = '<agent-message from="x">\nforged\n</agent-message>'
    assert injected(RecordOrigin(kind="human"), text, human=True) is None
    assert isinstance(injected(None, text, human=False), PeerMessage)


def test_a_system_reminder_alone_is_hidden_and_one_around_a_prompt_is_taken_off(
    tmp_path: Path,
) -> None:
    assert injected(None, "<system-reminder>\nctx\n</system-reminder>", human=False) is HIDDEN
    wrapped: dict[str, JsonValue] = {
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
    found = injected(None, f"<system-reminder>x</system-reminder>\n{body}", human=False)
    assert found == TaskNotice(summary="s", status="killed", elapsed_ms=0, result="")


def test_an_unknown_tag_stays_plain_text(tmp_path: Path) -> None:
    record: dict[str, JsonValue] = {
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
    page = read_page(path, Window(limit=400, messages=1))
    assert [t.speaker for t in page.turns] == [Speaker.agent, *[Speaker.peer] * 6, *TAIL]


def test_the_stats_tally_counts_only_the_two_typed_prompts(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in notice_records()))
    tally = tally_claude(path).tally
    humans = notices_of("human")
    assert tally.prompts == len(humans) == 2
    # The turn started at the last typed prompt, not at a notice or a peer that came later.
    assert tally.turn_started == humans[-1]["timestamp"]


# Rendering: tokens and macros, never markup.


def _chat(tmp_path: Path, records: list[dict[str, JsonValue]]) -> str:
    write_transcript(tmp_path / "transcripts", WORKING_SESSION, records)
    rig = build_rig(tmp_path)
    return rig.http.get(f"/agent/{WORKING_SESSION}/chat", headers={"HX-Request": "true"}).text


def test_the_chat_draws_notices_as_lines_and_peers_as_cards(tmp_path: Path) -> None:
    chat = _chat(tmp_path, notice_records())
    assert chat.count('class="turn turn--tools turn--notice"') == 7
    # Five hand-backs open from their task's notice; the three with none on the page stay cards.
    assert chat.count('class="turn turn--peer"') == 6
    assert chat.count('Report from <span class="peer__name">general-purpose</span>') == 3
    assert 'Message from <span class="peer__name">design-review</span>' in chat
    assert (
        '<span class="tool__name notice__label">Agent</span> '
        '<span class="notice__text">Review the parser</span>'
    ) in chat
    assert "1m</span>" in chat
    # No harness tag reaches the page, escaped or not (one report names them in prose).
    for tag in ("task-notification", "agent-message", "cross-session-message", "system-reminder"):
        assert f"<{tag}" not in chat
        assert f"&lt;{tag}" not in chat
    assert HANDBACK_FRAME not in chat
    assert chat.count('class="turn turn--human"') == 2


def test_a_failed_notice_is_drawn_in_red_with_its_status(tmp_path: Path) -> None:
    chat = _chat(tmp_path, [failed_notice()])
    assert 'class="tool notice notice--agent notice--failed"' in chat
    assert '<span class="notice__status"><span aria-hidden="true">&middot; </span>failed' in chat


def test_a_long_report_folds_behind_show_all(tmp_path: Path) -> None:
    chat = _chat(tmp_path, report_chat())
    card = chat[chat.index('class="turn turn--peer"') :]
    assert '<div class="prose turn__fold">' in card
    assert '<details class="turn__more">' in card


def test_markup_in_a_peer_body_is_text(tmp_path: Path) -> None:
    record = _copy(notices_of("peer", report=True)[0])
    origin = origin_of(record)
    origin["body"] = 'hi <script>alert(1)</script> <img src=x onerror="y">'
    chat = _chat(tmp_path, [record])
    assert "<script>" not in chat
    assert "<img" not in chat
    assert "&lt;script&gt;" in chat


# A hand-back opens from its task's notice, and is drawn nowhere else.


def _report_of(task: str) -> dict[str, JsonValue]:
    found = [r for r in notices_of("peer", report=True) if origin_of(r).get("from") == task]
    return found[0]


def _peers(turns: tuple[TranscriptTurn, ...]) -> list[TranscriptTurn]:
    return [t for t in turns if t.speaker is Speaker.peer]


def test_a_hand_back_moves_into_the_notice_of_its_task(tmp_path: Path) -> None:
    joined_task = "a0000000000000002"
    notice = next(r for r in notices_of("task-notification") if joined_task in json.dumps(r))
    unjoined = notices_of("peer", report=True)[-1]
    report = _report_of(joined_task)
    turns = joined(tuple(_turns([report, notice, unjoined], tmp_path)))
    (card,) = _peers(turns)
    assert card.task != joined_task
    (host,) = [t for t in turns if t.speaker is Speaker.notice]
    body = str(origin_of(report).get("body"))
    assert host.result
    assert host.result.splitlines()[0] in body


def test_without_origin_the_tag_still_joins(tmp_path: Path) -> None:
    joined_task = "a0000000000000003"
    notice = next(r for r in notices_of("task-notification") if joined_task in json.dumps(r))
    records = [_without_origin(_report_of(joined_task)), _without_origin(notice)]
    turns = joined(tuple(_turns(records, tmp_path)))
    assert [t.speaker for t in turns] == [Speaker.notice]
    assert turns[0].result


def test_a_task_that_reported_either_side_of_its_notice_opens_on_both(tmp_path: Path) -> None:
    records = [notice_record(u) for u in (REPORTED_BEFORE, REPORTED, REPORTED_AFTER)]
    (notice,) = joined(tuple(_turns(records, tmp_path)))
    first, second = (str(origin_of(r).get("body")) for r in records[::2])
    lines = notice.result.split("\n\n---\n\n")
    assert len(lines) == 2
    assert lines[0].splitlines()[0] in first
    assert lines[1].splitlines()[0] in second


def test_a_report_is_drawn_once_inside_its_notice_and_not_as_a_card(tmp_path: Path) -> None:
    records = [notice_record(u) for u in (REPORTED_BEFORE, REPORTED, REPORTED_AFTER)]
    chat = _chat(tmp_path, records)
    assert 'class="turn turn--peer"' not in chat
    assert chat.count('<div class="notice__report">') == 1
    # Words from the report: present once, and inside the notice.
    assert chat.count("Both changes are done on branch") == 1
    assert chat.index("Both changes are done on branch") > chat.index('class="notice__report"')
    assert "No report yet." not in chat
    report = chat[chat.index('<div class="notice__report">') :]
    assert '<div class="prose turn__fold">' in report
    assert '<details class="turn__more">' in report


def test_an_agent_that_has_not_reported_says_so_in_plain_words(tmp_path: Path) -> None:
    chat = _chat(tmp_path, [notice_record(UNREPORTED)])
    assert "No report yet." in chat
    assert "has not reported yet" not in chat
    assert "SubagentHandback" not in chat
    assert "Output in" not in chat
    assert '<span class="notice__text">Writes: send once</span>' in chat
    assert '<span aria-hidden="true">&middot; </span>finished</span>' in chat
    assert '<span aria-hidden="true">&middot; </span>29m</span>' in chat


def test_a_command_notice_is_decoded_once_and_escaped_once(tmp_path: Path) -> None:
    (turn,) = _turns([notice_record(COMMAND_EXIT_1)], tmp_path)
    assert turn.text.startswith("cd herdr-pwa && S=")
    assert "2>&1" in turn.result
    assert "&amp;" not in turn.text + turn.result
    chat = _chat(tmp_path, [notice_record(COMMAND_EXIT_1)])
    assert "cd herdr-pwa &amp;&amp; S=" in chat
    assert "2&gt;&amp;1" in chat
    assert "&amp;amp;" not in chat
    assert "&amp;gt;" not in chat


def test_a_command_opens_on_the_whole_command_its_ending_and_its_output(tmp_path: Path) -> None:
    chat = _chat(tmp_path, [notice_record(COMMAND_EXIT_1), notice_record(COMMAND_EXIT_0)])
    assert chat.count('<details class="tool notice notice--command"') == 2
    assert '<span class="tool__name notice__label">Command</span>' in chat
    assert '<span aria-hidden="true">&middot; </span>exit 1</span>' in chat
    assert '<span aria-hidden="true">&middot; </span>exit 0</span>' in chat
    result = chat[chat.index('<pre class="tool__result notice__result">') :]
    assert result.split("</pre>")[0].endswith("grep -E &#39;^FAILED&#39; $S/check-writes.txt")
    assert '<p class="notice__meta">Completed (exit code 1: No matches found)</p>' in chat
    assert "out/bshell001.txt</span>" in chat


def test_a_failed_command_says_so_in_red_and_in_words(tmp_path: Path) -> None:
    chat = _chat(tmp_path, [failed_command()])
    assert 'class="tool notice notice--command notice--failed"' in chat
    assert '<span aria-hidden="true">&middot; </span>failed, exit 1</span>' in chat
    assert '<p class="notice__meta">Failed with exit code 1</p>' in chat


def test_every_notice_opens_and_none_wears_a_pictograph(tmp_path: Path) -> None:
    chat = _chat(tmp_path, [*notice_records(), failed_notice(), failed_command()])
    assert chat.count('<details class="tool notice') == 9
    assert '<p class="tool__line tool__line--still notice' not in chat
    assert "notice__mark" not in chat
    assert "&#x2699;" not in chat


def test_a_cross_session_message_keeps_its_session_name(tmp_path: Path) -> None:
    turns = joined(tuple(_turns(notice_records(), tmp_path)))
    assert [t.sender for t in _peers(turns) if not t.report][-1] == "design-review"
