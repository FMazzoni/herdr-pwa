"""Prompts the person sent while a turn ran: drawn once, where the agent took them; until then,
one line saying how many wait."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import JsonValue

from herdr_pwa.engine import Speaker, TranscriptPage
from tests.catalogue import QUEUED, queued_records, queued_repeat_records
from tests.engine_seam import Window, fold_page, read_page
from tests.webfakes import WORKING_SESSION, build_rig, write_transcript

SKIP = "Skip the browser tests for now."
CHANGELOG = "Now update the changelog."
PASTED = "failing: test_tail.py::test_extend\nAssertionError: 3 != 4"
PULLED = "Rename the helper while you are there."
WAITING = "Also run the linter once that passes."
AGAIN = "Check the status again."


def _page(records: list[dict[str, JsonValue]], tmp_path: Path) -> TranscriptPage:
    path = tmp_path / "log.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return fold_page(read_page(path, Window(limit=400)))


def _said(page: TranscriptPage) -> list[tuple[str, str]]:
    """Who said what, in drawing order; tool lines and notices named by their kind alone."""
    return [
        (t.speaker.value, t.text if t.speaker in (Speaker.human, Speaker.agent) else "")
        for t in page.turns
    ]


def _chat(tmp_path: Path, records: list[dict[str, JsonValue]]) -> str:
    write_transcript(tmp_path / "transcripts", WORKING_SESSION, records)
    rig = build_rig(tmp_path)
    return rig.http.get(f"/agent/{WORKING_SESSION}/chat", headers={"HX-Request": "true"}).text


def _row(tmp_path: Path, records: list[dict[str, JsonValue]]) -> str:
    write_transcript(tmp_path / "transcripts", WORKING_SESSION, records)
    rig = build_rig(tmp_path)
    return rig.http.get(f"/agent/{WORKING_SESSION}/tray", headers={"HX-Request": "true"}).text


def test_the_fixture_holds_each_queue_shape() -> None:
    records = queued_records()
    operations = [r.get("operation") for r in records if r.get("type") == "queue-operation"]
    assert operations.count("enqueue") == 6
    assert operations.count("remove") == 3
    assert operations.count("dequeue") == 1
    assert operations.count("popAll") == 1
    assert len([r for r in records if r.get("promptSource") == "queued"]) == 1
    assert "/home/" not in QUEUED.read_text().replace("/home/user/", "")


def test_each_prompt_is_drawn_once_where_the_agent_took_it(tmp_path: Path) -> None:
    assert _said(_page(queued_records(), tmp_path)) == [
        ("human", "Run the chat tests and fix whatever fails."),
        ("tool", ""),
        ("tool", ""),
        ("human", SKIP),
        ("notice", ""),
        ("agent", "Both suites pass; the browser tests are skipped as you asked."),
        ("human", CHANGELOG),
        ("tool", ""),
        ("tool", ""),
        ("human", PASTED),
        (
            "agent",
            "The changelog is updated, and the tail test you pasted fails on the extend case.",
        ),
        ("human", "Look at this:\n\nTraceback: KeyError: 'at'\n\nis it the same bug?"),
        ("tool", ""),
        ("tool", ""),
    ]


def test_only_a_prompt_not_yet_taken_waits(tmp_path: Path) -> None:
    # Absorbed, dequeued and pulled back to edit are all over; only the last one still waits.
    assert _page(queued_records(), tmp_path).queued == [WAITING]


def test_a_queue_record_is_never_a_turn(tmp_path: Path) -> None:
    page = _page(queued_records(), tmp_path)
    assert all(t.speaker is not Speaker.queue for t in page.turns)
    assert all(PULLED not in t.text and WAITING not in t.text for t in page.turns)


def test_a_paste_is_drawn_without_the_tags_the_harness_wrapped_it_in(tmp_path: Path) -> None:
    for turn in _page(queued_records(), tmp_path).turns:
        assert "pasted_content" not in turn.text


def test_the_same_words_sent_again_are_another_prompt(tmp_path: Path) -> None:
    page = _page(queued_repeat_records(), tmp_path)
    humans = [t.text for t in page.turns if t.speaker is Speaker.human]
    # Typed, then queued and taken: two prompts. Queued once more and not taken: waiting, not drawn.
    assert humans == [AGAIN, "Fix it, then report.", AGAIN]
    assert page.queued == [AGAIN]


def test_the_chat_draws_each_prompt_exactly_once(tmp_path: Path) -> None:
    chat = _chat(tmp_path, queued_records())
    assert chat.count('class="turn turn--human"') == 5
    for text in (SKIP, CHANGELOG, "Run the chat tests and fix whatever fails."):
        assert chat.count(text) == 1
    assert chat.count("AssertionError: 3 != 4") == 1
    assert PULLED not in chat
    assert "pasted_content" not in chat


def test_the_row_says_how_many_wait_and_its_sheet_lists_them(tmp_path: Path) -> None:
    row = _row(tmp_path, queued_records())
    assert '<span class="tray__count" aria-hidden="true">&middot; 1 queued</span>' in row
    assert '<p class="queued__head">1 message queued</p>' in row
    assert f'<li class="queued__text">{WAITING}</li>' in row
    assert row.count(WAITING) == 1
    assert WAITING not in _chat(tmp_path / "log", queued_records())
    later = {**queued_records()[-1], "content": "And push the branch.", "timestamp": "later"}
    two = [*queued_records(), later]
    assert ">2 messages queued<" in _row(tmp_path / "two", two)


def test_nothing_waiting_draws_no_line(tmp_path: Path) -> None:
    records = [r for r in queued_records() if r.get("content") != WAITING]
    assert 'class="queued"' not in _row(tmp_path, records)


def test_a_waiting_prompt_is_escaped_text(tmp_path: Path) -> None:
    records = [*queued_records()]
    records[-1] = {**records[-1], "content": "<b>bold</b> & <script>x</script>"}
    row = _row(tmp_path, records)
    assert "&lt;b&gt;bold&lt;/b&gt; &amp; &lt;script&gt;x&lt;/script&gt;" in row
    assert "<script>x" not in row
