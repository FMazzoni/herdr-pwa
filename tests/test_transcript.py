"""The transcript reader, against a transcript nobody wrote by hand.

Every assertion here runs on ``tests/fixtures/transcript/claude/`` -- a real
Claude Code JSONL file, captured verbatim off disk from a live agent in the
isolated ``hp-test`` session on 2026-08-19. The recap fix is the reason: a grammar
designed against an imagined screen once drove a safety decision the wrong way,
so a fixture in this repo is a capture or it is not a fixture.

What the fixture is, exactly: a haiku agent in ``/tmp``, given two prompts --
one prose question, one ``echo`` -- producing 23 records that between them cover
every shape the reader has to deal with: a typed human prompt, ``thinking``,
``text``, ``tool_use``, ``tool_result``, four ``attachment`` records, a
``system`` turn duration, and the ``mode`` / ``permission-mode`` / ``ai-title`` /
``atis-latch`` / ``file-history-snapshot`` sidecars.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from herdr_pwa.engine.transcript import (
    AGENT_KINDS,
    MAX_NOTE_CHARS,
    MAX_PROSE_CHARS,
    MAX_RESULT_CHARS,
    HistoryState,
    Speaker,
    TranscriptPage,
    adapter_for,
    history,
    locate,
    read_page,
)

FIXTURES = Path(__file__).parent / "fixtures" / "transcript"
SESSION = "00000000-0000-4000-8000-000000000153"
TRANSCRIPT = FIXTURES / "claude" / f"{SESSION}.jsonl"


@pytest.fixture
def transcript_root(tmp_path: Path) -> Path:
    """The fixture laid out the way Claude Code lays a real tree out.

    The directory name is a *lossy* encoding of the session's startup cwd, which
    is exactly why the reader globs. Reproducing the encoding here keeps the
    test honest about what it is exercising.
    """
    project = tmp_path / "-tmp"
    project.mkdir()
    (project / f"{SESSION}.jsonl").write_bytes(TRANSCRIPT.read_bytes())
    return tmp_path


# ---------------------------------------------------------------------------
# The fixture itself. If these fail the capture has been edited, and every
# assertion below is measuring something other than what it says.
# ---------------------------------------------------------------------------


def test_fixture_is_one_json_object_per_line() -> None:
    """4269 lines parsed to 4269 records on the 13 MB original; hold the shape.

    This is the property the byte-cursor paging depends on: a line boundary is a
    record boundary, so seeking to one and parsing forward cannot land mid-record.
    """
    lines = TRANSCRIPT.read_text().splitlines()
    assert lines
    for line in lines:
        assert isinstance(json.loads(line), dict)


def test_fixture_covers_every_shape_the_reader_branches_on() -> None:
    kinds = {json.loads(line)["type"] for line in TRANSCRIPT.read_text().splitlines()}
    assert {"user", "assistant", "attachment", "system"} <= kinds
    blocks = {
        block["type"]
        for line in TRANSCRIPT.read_text().splitlines()
        for block in (json.loads(line).get("message") or {}).get("content", [])
        if isinstance(block, dict)
    }
    assert {"thinking", "text", "tool_use", "tool_result"} <= blocks


# ---------------------------------------------------------------------------
# locate
# ---------------------------------------------------------------------------


def test_locate_finds_the_file_by_uuid_alone(transcript_root: Path) -> None:
    """No cwd is supplied, and none is needed: the uuid is unique in the tree."""
    found = locate(SESSION, root=transcript_root)
    assert found is not None
    assert found.name == f"{SESSION}.jsonl"


def test_locate_is_none_when_the_provider_wrote_nothing(tmp_path: Path) -> None:
    """Observed for real: a child session writes no transcript and says so only
    in a pane footer. That is an ordinary state, not an error."""
    assert locate(SESSION, root=tmp_path) is None


def test_locate_does_not_ignore_a_subagent_directory_by_accident(
    transcript_root: Path,
) -> None:
    """A subagent's transcript is a different conversation and lives one level
    deeper. The glob is one level, so it must not be picked up."""
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


# ---------------------------------------------------------------------------
# read_page: what actually comes out
# ---------------------------------------------------------------------------


def test_the_page_is_a_conversation_not_a_screen() -> None:
    page = read_page(TRANSCRIPT, limit=100)
    speakers = [turn.speaker for turn in page.turns]
    assert Speaker.human in speakers
    assert Speaker.agent in speakers
    assert Speaker.tool in speakers
    assert not page.has_more
    assert page.cursor == 0


def test_turns_come_back_oldest_first_and_in_file_order() -> None:
    page = read_page(TRANSCRIPT, limit=100)
    order = [
        json.loads(line).get("uuid")
        for line in TRANSCRIPT.read_text().splitlines()
        if json.loads(line).get("uuid")
    ]
    got = [turn.uuid for turn in page.turns]
    assert got == [uuid for uuid in order if uuid in set(got)]


def test_the_human_turns_are_the_two_prompts_that_were_typed() -> None:
    page = read_page(TRANSCRIPT, limit=100)
    typed = [turn.text for turn in page.turns if turn.speaker is Speaker.human]
    assert typed == [
        "Reply with one short sentence about the colour of the sky. Do not use any tools.",
        "Now run: echo hello-from-agent",
    ]


def test_a_tool_result_is_not_filed_as_the_human(transcript_root: Path) -> None:
    """The trap this reader exists to avoid. Claude Code files a tool result as
    ``role: "user"`` -- 719 of 818 user records in the 13 MB original -- so
    rendering by role puts the agent's own shell output in the person's column.
    """
    page = read_page(TRANSCRIPT, limit=100)
    human = " ".join(t.text for t in page.turns if t.speaker is Speaker.human)
    assert "hello-from-agent\n" not in human
    results = [t for t in page.turns if t.speaker is Speaker.tool and t.tool == "result"]
    assert results
    assert any("hello-from-agent" in t.text for t in results)


def test_a_tool_call_carries_its_name() -> None:
    page = read_page(TRANSCRIPT, limit=100)
    calls = [t for t in page.turns if t.speaker is Speaker.tool and t.tool != "result"]
    assert [t.tool for t in calls] == ["Bash"]


def test_bookkeeping_records_never_reach_the_chat() -> None:
    """4 attachments, a turn-duration system record and five sidecar types are in
    this file; none of them is anything a person would call a message."""
    page = read_page(TRANSCRIPT, limit=100)
    text = " ".join(turn.text for turn in page.turns)
    assert "deferred_tools_delta" not in text
    assert "total_tokens_reminder" not in text
    assert "trackedFileBackups" not in text


def test_every_turn_has_a_stable_id_to_append_by() -> None:
    """An append-only view re-reads an overlapping page and must drop duplicates
    without comparing text. The provider's uuid is what it keys on."""
    page = read_page(TRANSCRIPT, limit=100)
    uuids = [turn.uuid for turn in page.turns]
    assert all(uuids)
    assert len(uuids) == len(set(uuids))


# ---------------------------------------------------------------------------
# Paging upward, bounded
# ---------------------------------------------------------------------------


def test_paging_upward_walks_the_whole_file_without_repeating() -> None:
    whole = read_page(TRANSCRIPT, limit=100)
    collected: list[str] = []
    page = TranscriptPage(cursor=TRANSCRIPT.stat().st_size, has_more=True)
    guard = 0
    before: int | None = None
    while guard < 50:
        guard += 1
        page = read_page(TRANSCRIPT, limit=2, before=before)
        if not page.turns and not page.has_more:
            break
        collected = [turn.uuid for turn in page.turns] + collected
        if not page.has_more:
            break
        before = page.cursor
    assert collected == [turn.uuid for turn in whole.turns]


def test_a_page_never_reads_more_than_its_byte_budget(tmp_path: Path) -> None:
    """The bound that matters: the largest transcript on the capture box was
    28 MB and its longest single line 1 MB, so a page has to be capped in bytes
    and not only in turns."""
    fat = tmp_path / "fat.jsonl"
    noise = json.dumps({"type": "attachment", "attachment": {"pad": "x" * 20000}})
    fat.write_text("\n".join([noise] * 500) + "\n")
    page = read_page(fat, limit=40, max_bytes=200_000)
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
    page = read_page(big, limit=5)
    assert len(page.turns) == 1
    assert page.turns[0].truncated
    assert len(page.turns[0].text) == MAX_PROSE_CHARS


def test_a_tool_result_gets_a_gist_and_prose_gets_room(tmp_path: Path) -> None:
    """The two caps are different NUMBERS and different JOBS.

    One flat cap was wrong in both directions at once: it let a megabyte of
    ``cat`` output take four thousand characters of a metered page, and it cut
    the model's answer -- the thing the tab exists for -- at the same number.
    """
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
    page = read_page(mixed, limit=9)
    by_uuid = {turn.uuid: turn for turn in page.turns}
    assert len(by_uuid["res"].text) == MAX_RESULT_CHARS
    assert by_uuid["res"].truncated
    # 9 000 characters of prose is past the OLD flat cap and well inside the new
    # one: the answer arrives whole rather than ending mid-sentence.
    assert by_uuid["prose"].text == "y" * 9000
    assert not by_uuid["prose"].truncated
    assert MAX_RESULT_CHARS < MAX_PROSE_CHARS


def test_injected_context_is_capped_at_what_can_be_seen(tmp_path: Path) -> None:
    """A system turn renders clamped to three lines and has no disclosure, so
    the cap is what a person could ever look at -- not what prose gets."""
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
    page = read_page(injected, limit=5)
    assert page.turns[0].speaker is Speaker.system
    assert len(page.turns[0].text) == MAX_NOTE_CHARS
    assert page.turns[0].truncated


def test_a_torn_final_line_is_skipped_rather_than_raised(tmp_path: Path) -> None:
    """A transcript is appended to while it is read. A chat that 500s because the
    agent was mid-write is worse than a chat one turn behind."""
    torn = tmp_path / "torn.jsonl"
    good = TRANSCRIPT.read_text()
    torn.write_text(good + '{"type": "assistant", "uuid": "half", "mess')
    page = read_page(torn, limit=100)
    assert page.turns
    assert "half" not in {turn.uuid for turn in page.turns}


def test_a_torn_final_line_does_not_take_the_live_cursor_with_it(
    tmp_path: Path,
) -> None:
    """Skipping the torn record is only half of it: ``end`` is what the
    view asks from next, so declaring it at the end of the FILE says those bytes
    were delivered when they were dropped. The rest of the record then lands
    below the cursor and no read ever reaches it."""
    torn = tmp_path / "torn.jsonl"
    good = TRANSCRIPT.read_text()
    torn.write_text(good + '{"type": "assistant", "uuid": "half", "mess')

    page = read_page(torn, limit=100)

    assert page.end == len(good.encode()), "the cursor stepped over a partial record"
    assert page.size == torn.stat().st_size, "size is the file, end is the frontier"


def test_a_torn_line_longer_than_one_read_still_holds_the_cursor(
    tmp_path: Path,
) -> None:
    """The longest single line measured on a real transcript was 1 067 853 bytes,
    so the walk back to the last whole line cannot assume one chunk is enough."""
    torn = tmp_path / "torn.jsonl"
    good = TRANSCRIPT.read_text()
    torn.write_text(good + '{"type": "assistant", "uuid": "half", "x": "' + "y" * 900_000)

    page = read_page(torn, limit=100)

    assert page.end == len(good.encode())


def test_a_file_holding_no_whole_line_yet_delivers_no_cursor(tmp_path: Path) -> None:
    """The degenerate case of the same rule: the very first record is still
    being written, so nothing has been delivered and the frontier is byte zero."""
    partial = tmp_path / "partial.jsonl"
    partial.write_text('{"type": "user", "uuid": "h1", "mess')

    page = read_page(partial, limit=100)

    assert page.turns == []
    assert page.end == 0


# ---------------------------------------------------------------------------
# history(): the door the app uses
# ---------------------------------------------------------------------------


def test_history_reads_the_real_file_through_the_registry(
    transcript_root: Path,
) -> None:
    page = history(SESSION, agent_kind="claude", root=transcript_root)
    assert [t.speaker for t in page.turns].count(Speaker.human) == 2


def test_a_provider_with_no_adapter_simply_has_no_history(
    transcript_root: Path,
) -> None:
    """The provider rule, and cursor is now the case that proves it: ~/.cursor was
    searched on 2026-08-20 and holds no session log at all. opencode does keep
    one, in a store of its own, and tests/test_opencode.py is that reader."""
    assert adapter_for("cursor") is None
    assert history(SESSION, agent_kind="cursor", root=transcript_root).turns == []


def test_the_registry_and_the_kinds_agree() -> None:
    """AGENT_KINDS is what a qualified root entry may name, so it may not drift
    from the registry: a kind in one and not the other is a root entry silently
    searched by nobody, or an adapter nobody can point at a captured tree."""
    assert set(AGENT_KINDS) == {"claude", "opencode", "hermes"}
    assert all(adapter_for(kind) is not None for kind in AGENT_KINDS)


def test_a_qualified_root_is_searched_only_by_the_harness_it_names(
    transcript_root: Path,
) -> None:
    """Two providers, one setting: an entry may say which it is for."""
    entry = f"claude:{transcript_root}"
    assert history(SESSION, agent_kind="claude", root=entry).turns
    assert history(SESSION, agent_kind="opencode", root=entry).state is (HistoryState.absent)


def test_an_unqualified_root_replaces_every_provider_default(
    transcript_root: Path,
) -> None:
    """The override rule: pointing the reader at a fixture must not leave some
    other provider free to fall back to the real store on the machine."""
    page = history("ses_whatever", agent_kind="opencode", root=transcript_root)
    assert page.state is HistoryState.absent
    assert page.turns == []


def test_history_never_raises_on_a_missing_tree(tmp_path: Path) -> None:
    page = history(SESSION, agent_kind="claude", root=tmp_path / "nope")
    assert page == TranscriptPage()
