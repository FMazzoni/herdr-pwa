"""The transcript reader, against a transcript nobody wrote by hand."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import JsonValue, TypeAdapter

from herdr_pwa.engine import (
    HERDR_KINDS,
    HistoryState,
    Source,
    Speaker,
    TranscriptPage,
    harness_kinds,
    harness_of,
    has_byte_frontier,
    locate,
)
from tests.engine_seam import (
    MAX_NOTE_CHARS,
    MAX_PROSE_CHARS,
    MAX_RESULT_CHARS,
    ClaudeRecord,
    Window,
    adapter_for,
    claude_record,
    history,
    read_page,
)

FIXTURES = Path(__file__).parent / "fixtures" / "transcript"
SESSION = "00000000-0000-4000-8000-000000000153"
TRANSCRIPT = FIXTURES / "claude" / f"{SESSION}.jsonl"


@pytest.fixture
def transcript_root(tmp_path: Path) -> Path:
    """The fixture laid out the way Claude Code lays a real tree out."""
    project = tmp_path / "-tmp"
    project.mkdir()
    (project / f"{SESSION}.jsonl").write_bytes(TRANSCRIPT.read_bytes())
    return tmp_path


# The fixture: if these fail, the capture has been edited.


def test_fixture_is_one_json_object_per_line() -> None:
    """4269 lines parsed to 4269 records on the 13 MB original; hold the shape."""
    lines = TRANSCRIPT.read_text().splitlines()
    assert lines
    # The raw shape on disk, every field kept: this holds the capture, not the reader.
    line_shape = TypeAdapter(dict[str, JsonValue])
    for line in lines:
        assert line_shape.validate_json(line)


def _records() -> list[ClaudeRecord]:
    found = (claude_record(line.encode()) for line in TRANSCRIPT.read_text().splitlines())
    return [record for record in found if record is not None]


def test_fixture_covers_every_shape_the_reader_branches_on() -> None:
    kinds = {record.type for record in _records()}
    assert {"user", "assistant", "attachment", "system"} <= kinds
    blocks = {
        block.type
        for record in _records()
        if record.message is not None and isinstance(record.message.content, list)
        for block in record.message.content
        if block is not None
    }
    assert {"thinking", "text", "tool_use", "tool_result"} <= blocks


# locate


def test_locate_finds_the_file_by_uuid_alone(transcript_root: Path) -> None:
    """No cwd is supplied, and none is needed: the uuid is unique in the tree."""
    found = locate(SESSION, root=transcript_root)
    assert found is not None
    assert found.name == f"{SESSION}.jsonl"


def test_locate_is_none_when_the_provider_wrote_nothing(tmp_path: Path) -> None:
    """A child session that writes no transcript is an ordinary state, not an error."""
    assert locate(SESSION, root=tmp_path) is None


def test_locate_does_not_ignore_a_subagent_directory_by_accident(
    transcript_root: Path,
) -> None:
    """A subagent's transcript lives one level deeper and must not be picked up."""
    nested = transcript_root / "-tmp" / SESSION / "subagents"
    nested.mkdir(parents=True)
    (nested / "agent-abc.jsonl").write_text("{}\n")
    found = locate(SESSION, root=transcript_root)
    assert found is not None
    assert "subagents" not in found.parts


@pytest.mark.parametrize("hostile", ["../../etc/passwd", "..", ".hidden", "a/b", "a\\b", "", "   "])
def test_locate_refuses_a_session_id_that_is_really_a_path(
    transcript_root: Path, hostile: str
) -> None:
    """A session id arrives off a URL segment. It is a name, never a path."""
    assert locate(hostile, root=transcript_root) is None


# read_page: what actually comes out


def test_the_page_is_a_conversation_not_a_screen() -> None:
    page = read_page(TRANSCRIPT, Window(limit=100))
    speakers = [turn.speaker for turn in page.turns]
    assert Speaker.human in speakers
    assert Speaker.agent in speakers
    assert Speaker.tool in speakers
    assert not page.has_more
    assert page.cursor == 0


def test_turns_come_back_oldest_first_and_in_file_order() -> None:
    page = read_page(TRANSCRIPT, Window(limit=100))
    order = [record.uuid for record in _records() if record.uuid]
    got = [turn.uuid for turn in page.turns]
    assert got == [uuid for uuid in order if uuid in set(got)]


def test_the_human_turns_are_the_two_prompts_that_were_typed() -> None:
    page = read_page(TRANSCRIPT, Window(limit=100))
    typed = [turn.text for turn in page.turns if turn.speaker is Speaker.human]
    assert typed == [
        "Reply with one short sentence about the colour of the sky. Do not use any tools.",
        "Now run: echo hello-from-agent",
    ]


def test_a_tool_result_is_not_filed_as_the_human() -> None:
    """A tool result filed as ``role: "user"`` must not render in the person's column."""
    page = read_page(TRANSCRIPT, Window(limit=100))
    human = " ".join(t.text for t in page.turns if t.speaker is Speaker.human)
    assert "hello-from-agent\n" not in human
    results = [t for t in page.turns if t.speaker is Speaker.tool and t.tool == "result"]
    assert results
    assert any("hello-from-agent" in t.text for t in results)


def test_a_tool_call_carries_its_name() -> None:
    page = read_page(TRANSCRIPT, Window(limit=100))
    calls = [t for t in page.turns if t.speaker is Speaker.tool and t.tool != "result"]
    assert [t.tool for t in calls] == ["Bash"]


def test_bookkeeping_records_never_reach_the_chat() -> None:
    """Attachments, system records and sidecar types are not messages."""
    page = read_page(TRANSCRIPT, Window(limit=100))
    text = " ".join(turn.text for turn in page.turns)
    assert "deferred_tools_delta" not in text
    assert "total_tokens_reminder" not in text
    assert "trackedFileBackups" not in text


def test_every_turn_has_a_stable_id_to_append_by() -> None:
    """Overlapping pages dedupe on the provider's uuid, never on text."""
    page = read_page(TRANSCRIPT, Window(limit=100))
    uuids = [turn.uuid for turn in page.turns]
    assert all(uuids)
    assert len(uuids) == len(set(uuids))


# Paging upward, bounded


def test_paging_upward_walks_the_whole_file_without_repeating() -> None:
    whole = read_page(TRANSCRIPT, Window(limit=100))
    collected: list[str] = []
    page = TranscriptPage(cursor=TRANSCRIPT.stat().st_size, has_more=True)
    guard = 0
    before: int | None = None
    while guard < 50:
        guard += 1
        page = read_page(TRANSCRIPT, Window(limit=2, before=before))
        if not page.turns and not page.has_more:
            break
        collected = [turn.uuid for turn in page.turns] + collected
        if not page.has_more:
            break
        before = page.cursor
    assert collected == [turn.uuid for turn in whole.turns]


def test_a_page_never_reads_more_than_its_byte_budget(tmp_path: Path) -> None:
    """A page is capped in bytes, not only in turns: real lines reach 1 MB."""
    fat = tmp_path / "fat.jsonl"
    noise = json.dumps({"type": "attachment", "attachment": {"pad": "x" * 20000}})
    fat.write_text("\n".join([noise] * 500) + "\n")
    page = read_page(fat, Window(limit=40, max_bytes=200_000))
    assert page.turns == []
    assert page.has_more  # stopped early rather than reading the file


def test_an_oversized_turn_is_clipped_and_says_so(tmp_path: Path) -> None:
    big = tmp_path / "big.jsonl"
    big.write_text(
        json.dumps(
            {
                "type": "assistant",
                "uuid": "u1",
                "timestamp": "2026-08-19T00:00:00Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "y" * 50_000}],
                },
            }
        )
        + "\n"
    )
    page = read_page(big, Window(limit=5))
    assert len(page.turns) == 1
    assert page.turns[0].truncated
    assert len(page.turns[0].text) == MAX_PROSE_CHARS


def test_a_tool_result_gets_a_gist_and_prose_gets_room(tmp_path: Path) -> None:
    """The prose cap and the note cap are different numbers and different jobs."""
    mixed = tmp_path / "mixed.jsonl"
    mixed.write_text(
        "\n".join(
            json.dumps(record)
            for record in (
                {
                    "type": "assistant",
                    "uuid": "call",
                    "timestamp": "2026-08-19T00:00:00Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "tu1",
                                "name": "Read",
                                "input": {"file_path": "/big"},
                            }
                        ],
                    },
                },
                {
                    "type": "user",
                    "uuid": "res",
                    "timestamp": "2026-08-19T00:00:01Z",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "tu1",
                                "content": "z" * 500_000,
                            }
                        ],
                    },
                },
                {
                    "type": "assistant",
                    "uuid": "prose",
                    "timestamp": "2026-08-19T00:00:02Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "y" * 9000}],
                    },
                },
            )
        )
        + "\n"
    )
    page = read_page(mixed, Window(limit=9))
    by_uuid = {turn.uuid: turn for turn in page.turns}
    assert len(by_uuid["res"].text) == MAX_RESULT_CHARS
    assert by_uuid["res"].truncated
    # Past the old flat cap and inside the prose cap, so the answer arrives whole.
    assert by_uuid["prose"].text == "y" * 9000
    assert not by_uuid["prose"].truncated
    assert MAX_RESULT_CHARS < MAX_PROSE_CHARS


def test_injected_context_is_capped_at_what_can_be_seen(tmp_path: Path) -> None:
    """A system turn gets the note cap, not the prose cap."""
    injected = tmp_path / "injected.jsonl"
    injected.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "s1",
                "timestamp": "2026-08-19T00:00:00Z",
                "message": {"role": "user", "content": "n" * 50_000},
            }
        )
        + "\n"
    )
    page = read_page(injected, Window(limit=5))
    assert page.turns[0].speaker is Speaker.system
    assert len(page.turns[0].text) == MAX_NOTE_CHARS
    assert page.turns[0].truncated


def test_a_record_whose_fields_drifted_still_reaches_the_chat(tmp_path: Path) -> None:
    """A field the harness renamed, retyped or added reads as absent, never as a lost line."""
    drifted = {
        "type": "assistant",
        "uuid": "drifted",
        "timestamp": 1780000000,
        "isMeta": "no",
        "someFutureField": {"nested": [1, 2]},
        "message": {
            "role": "assistant",
            "usage": "not an object",
            "content": [
                {"type": "tool_use", "id": 7, "name": "Bash", "input": {"command": ["ls"]}},
                {"type": "text", "text": "still here", "citations": None},
            ],
        },
    }
    log = tmp_path / "drift.jsonl"
    log.write_text("not json\n" + json.dumps(drifted) + "\n[1, 2]\n")
    (turn,) = read_page(log, Window(limit=40)).turns
    assert (turn.uuid, turn.speaker, turn.ts, turn.call) == ("drifted", Speaker.tool, "", "")
    assert turn.tool == "Bash"


def test_a_torn_final_line_is_skipped_rather_than_raised(tmp_path: Path) -> None:
    """A record torn mid-write is skipped, not an error."""
    torn = tmp_path / "torn.jsonl"
    good = TRANSCRIPT.read_text()
    torn.write_text(good + '{"type": "assistant", "uuid": "half", "mess')
    page = read_page(torn, Window(limit=100))
    assert page.turns
    assert "half" not in {turn.uuid for turn in page.turns}


def test_a_torn_final_line_does_not_take_the_live_cursor_with_it(
    tmp_path: Path,
) -> None:
    """``end`` never claims the bytes of a torn record were delivered."""
    torn = tmp_path / "torn.jsonl"
    good = TRANSCRIPT.read_text()
    torn.write_text(good + '{"type": "assistant", "uuid": "half", "mess')

    page = read_page(torn, Window(limit=100))

    assert page.end == len(good.encode()), "the cursor stepped over a partial record"
    assert page.size == torn.stat().st_size, "size is the file, end is the frontier"


def test_a_torn_line_longer_than_one_read_still_holds_the_cursor(
    tmp_path: Path,
) -> None:
    """The walk back to the last whole line spans more than one chunk."""
    torn = tmp_path / "torn.jsonl"
    good = TRANSCRIPT.read_text()
    torn.write_text(good + '{"type": "assistant", "uuid": "half", "x": "' + "y" * 900_000)

    page = read_page(torn, Window(limit=100))

    assert page.end == len(good.encode())


def test_a_file_holding_no_whole_line_yet_delivers_no_cursor(tmp_path: Path) -> None:
    """With the first record still being written, the frontier is byte zero."""
    partial = tmp_path / "partial.jsonl"
    partial.write_text('{"type": "user", "uuid": "h1", "mess')

    page = read_page(partial, Window(limit=100))

    assert page.turns == []
    assert page.end == 0


# history(): the door the app uses


def test_history_reads_the_real_file_through_the_registry(
    transcript_root: Path,
) -> None:
    page = history(Source.of(SESSION, agent_kind="claude", root=transcript_root))
    assert [t.speaker for t in page.turns].count(Speaker.human) == 2


def test_a_provider_with_no_adapter_simply_has_no_history(
    transcript_root: Path,
) -> None:
    """A provider with no session log is unsupported, not absent."""
    assert adapter_for("cursor") is None
    assert history(Source.of(SESSION, agent_kind="cursor", root=transcript_root)).turns == []


def test_the_registry_is_keyed_by_each_adapters_own_kind() -> None:
    """One registry: the kinds are the adapters' own, and every one is a kind herdr runs."""
    assert harness_kinds() == ("claude", "opencode", "hermes")
    for kind in harness_kinds():
        harness = harness_of(kind)
        assert harness is not None
        assert harness.kind == kind
        assert adapter_for(kind) is not None
        assert kind in HERDR_KINDS


def test_only_a_byte_paged_log_is_grown_by_appended_bytes() -> None:
    """The tail and usage caches ask this; a kind with no adapter has no such log."""
    assert [k for k in harness_kinds() if has_byte_frontier(k)] == ["claude"]
    assert not has_byte_frontier("cursor")
    assert not has_byte_frontier(None)


def test_a_qualified_root_is_searched_only_by_the_harness_it_names(
    transcript_root: Path,
) -> None:
    """A root entry may name the provider it is for."""
    entry = f"claude:{transcript_root}"
    assert history(Source.of(SESSION, agent_kind="claude", root=entry)).turns
    assert history(Source.of(SESSION, agent_kind="opencode", root=entry)).state is (
        HistoryState.absent
    )


def test_an_unqualified_root_replaces_every_provider_default(
    transcript_root: Path,
) -> None:
    """An override leaves no provider free to fall back to the real store."""
    page = history(Source.of("ses_whatever", agent_kind="opencode", root=transcript_root))
    assert page.state is HistoryState.absent
    assert page.turns == []


def test_history_never_raises_on_a_missing_tree(tmp_path: Path) -> None:
    page = history(Source.of(SESSION, agent_kind="claude", root=tmp_path / "nope"))
    assert page == TranscriptPage()
