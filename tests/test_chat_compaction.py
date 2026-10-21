"""After /compact the chat shows one quiet line, opening on the summary; commands Claude Code
writes as system records show like any other; a failing command says so."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from pydantic import JsonValue

from herdr_pwa.decisions import context_stalled
from herdr_pwa.engine import Speaker, TranscriptTurn, is_slash_command
from tests._jsonpath import obj
from tests.catalogue import (
    api_error_records,
    command_stderr_records,
    compaction_records,
    slash_command_records,
    system_command_records,
)
from tests.engine_seam import (
    MAX_RESULT_CHARS,
    Window,
    fold_turns,
    is_message,
    parse_claude_line,
    read_page,
)
from tests.webfakes import WORKING_SESSION, build_rig, human_says, write_transcript

ARGS = "keep the parser notes and any useful tooling"
COMPACT = f"/compact {ARGS}"
CONTINUED = "This session is being continued from a previous conversation"
COMPACTED = "Compacted (ctrl+o to see full summary)"
# Claude Code's own words on the boundary record it writes before the summary.
SUMMARY_LINE = "Conversation compacted"


def _content(record: dict[str, JsonValue]) -> str:
    content = obj(record, "message").get("content") if "message" in record else None
    return content if isinstance(content, str) else str(record.get("content", ""))


def _folded(tmp_path: Path, records: list[dict[str, JsonValue]]) -> list[TranscriptTurn]:
    path = tmp_path / "log.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return fold_turns(read_page(path, Window(limit=400)).turns)


def _parsed(records: list[dict[str, JsonValue]]) -> list[TranscriptTurn]:
    parsed = (parse_claude_line(json.dumps(r).encode()) for r in records)
    return [turn for turn in parsed if turn is not None]


def _is_summary(record: dict[str, JsonValue]) -> bool:
    return record.get("isCompactSummary") is True


def _first_run() -> list[dict[str, JsonValue]]:
    """The first /compact in the fixture: as sent, the boundary, the summary, the command, output."""
    records = compaction_records()
    return records[:6]


def _summary_text(record: dict[str, JsonValue]) -> str:
    return _content(record).strip()[:MAX_RESULT_CHARS]


# The fixture: real shapes, scrubbed.


def test_the_fixture_holds_a_compact_as_claude_code_writes_it() -> None:
    records = _first_run()
    said = [_content(r) for r in records]
    # Measured: the line as typed, with no prompt source, before anything else.
    assert said[0] == COMPACT
    assert "promptSource" not in records[0]
    assert (records[1].get("type"), records[1].get("subtype")) == ("system", "compact_boundary")
    assert _is_summary(records[2])
    assert said[2].startswith(CONTINUED)
    assert len(said[2]) > MAX_RESULT_CHARS
    assert records[3].get("isMeta") is True
    assert said[4].startswith("<command-name>/compact</command-name>")
    assert f"<command-args>{ARGS}</command-args>" in said[4]
    assert said[5].startswith("<local-command-stdout>")


def test_the_fixture_holds_the_agents_last_words_between_summary_and_command() -> None:
    records = compaction_records()[6:]
    kinds = [str(r.get("type")) for r in records]
    assert kinds == ["system", "user", "assistant", "system", "user", "user", "user"]
    assert _is_summary(records[1])


# Reading: one line, opening on the summary.


def test_a_compact_is_one_ran_line_opening_on_its_summary(tmp_path: Path) -> None:
    records = _first_run()
    turns = _folded(tmp_path, records)
    assert [(t.speaker, t.tool, t.text) for t in turns] == [(Speaker.notice, "Ran", COMPACT)]
    (turn,) = turns
    assert turn.result == _summary_text(records[2])
    assert turn.result_truncated
    assert turn.uuid == records[4].get("uuid")


def test_the_line_as_sent_is_hidden_when_the_command_follows(tmp_path: Path) -> None:
    turns = _folded(tmp_path, compaction_records())
    assert not any(t.speaker is Speaker.system for t in turns)
    assert not any(t.text == COMPACT and not is_slash_command(t) for t in turns)


def test_the_summary_is_never_drawn_as_words(tmp_path: Path) -> None:
    turns = _folded(tmp_path, compaction_records())
    for turn in turns:
        if CONTINUED in turn.text:
            raise AssertionError(f"summary drawn as a line: {turn.speaker}")
        if CONTINUED in turn.result:
            assert (turn.speaker, turn.tool) == (Speaker.notice, "Ran")


def test_a_summary_reaches_past_the_agents_kept_words_to_its_command(tmp_path: Path) -> None:
    records = compaction_records()[6:]
    turns = _folded(tmp_path, records)
    assert [(t.speaker, t.tool) for t in turns] == [(Speaker.agent, ""), (Speaker.notice, "Ran")]
    assert turns[1].result == _summary_text(records[1])


def test_the_line_as_sent_stays_while_the_compact_is_still_running(tmp_path: Path) -> None:
    # Measured: it is written the moment the person sends, minutes before the rest.
    (turn,) = _folded(tmp_path, _first_run()[:1])
    assert (turn.speaker, turn.text) == (Speaker.system, COMPACT)


def test_a_summary_with_no_command_after_it_is_a_quiet_line_of_its_own(tmp_path: Path) -> None:
    records = _first_run()[1:3]
    (turn,) = _folded(tmp_path, records)
    assert (turn.speaker, turn.tool, turn.text) == (Speaker.notice, "Summary", SUMMARY_LINE)
    assert turn.result == _summary_text(records[1])
    assert not is_message(turn)
    assert not is_slash_command(turn)


def test_a_summary_is_not_folded_onto_a_command_past_a_prompt(tmp_path: Path) -> None:
    records = _first_run()
    turns = _folded(tmp_path, [*records[1:3], human_says("carry on", "typed"), *records[3:]])
    assert [(t.speaker, t.tool) for t in turns] == [
        (Speaker.notice, "Summary"),
        (Speaker.human, ""),
        (Speaker.notice, "Ran"),
    ]
    assert turns[2].result == COMPACTED


def test_a_summary_is_not_folded_onto_another_command(tmp_path: Path) -> None:
    records = _first_run()
    model = [r for r in slash_command_records() if "/model" in _content(r)]
    turns = _folded(tmp_path, [*records[1:3], *model])
    assert [(t.tool, t.text) for t in turns] == [("Summary", SUMMARY_LINE), ("Ran", "/model")]


def test_a_note_that_only_looks_like_the_command_is_kept(tmp_path: Path) -> None:
    records = _first_run()
    bare = copy.deepcopy(records[0])
    obj(bare, "message")["content"] = "/compact something else"
    turns = _folded(tmp_path, [bare, *records[1:]])
    assert [(t.speaker, t.text) for t in turns] == [
        (Speaker.system, "/compact something else"),
        (Speaker.notice, COMPACT),
    ]


# The stall: the person's /compact ends it; a summary is not the person acting.


def _refused() -> dict[str, JsonValue]:
    return next(r for r in api_error_records() if r.get("error") == "invalid_request")


def test_the_compact_as_sent_ends_a_full_context_stall_at_once() -> None:
    turns = _parsed([_refused(), _first_run()[0]])
    assert context_stalled(turns[:1]) is True
    assert context_stalled(turns) is False
    assert context_stalled(fold_turns(turns)) is False


def test_a_whole_compact_ends_a_full_context_stall() -> None:
    turns = _parsed([_refused(), *_first_run()])
    assert context_stalled(turns) is False
    assert context_stalled(fold_turns(turns)) is False


def test_a_summary_alone_does_not_end_a_full_context_stall() -> None:
    # Neither the person acting nor the agent speaking: the harness, looked past as a retry is.
    turns = _parsed([_refused(), *_first_run()[1:3]])
    assert [t.tool for t in turns] == ["Context", "Summary"]
    assert context_stalled(turns) is True
    assert context_stalled(fold_turns(turns)) is True


def test_a_summary_is_not_a_message() -> None:
    turns = _parsed(compaction_records())
    summaries = [t for t in turns if t.tool == "Summary"]
    assert len(summaries) == 2
    assert not any(is_message(t) or t.speaker is Speaker.human for t in summaries)


# Slash commands written as system records.


def test_a_command_written_as_a_system_record_is_a_ran_line(tmp_path: Path) -> None:
    records = system_command_records()
    turns = _folded(tmp_path, records)
    assert [(t.tool, t.text) for t in turns] == [
        ("Ran", "/artifacts"),
        ("Ran", "/feedback"),
        ("Ran", "/branch"),
        ("Ran", "/remote-control example"),
        ("Ran", "/login"),
    ]
    assert all(is_slash_command(t) and not t.failed for t in turns)
    commands = [r.get("uuid") for r in records if _content(r).startswith("<command-name>")]
    assert [t.uuid for t in turns] == commands


def test_what_a_system_record_command_printed_is_its_result(tmp_path: Path) -> None:
    turns = _folded(tmp_path, system_command_records())
    results = [t.result for t in turns]
    assert results[0] == 'Attached ⧉ "Example Guide" — Claude keeps track of new versions'
    assert results[1].startswith("Feedback sent")
    assert results[2].startswith("Branched conversation.")
    assert results[3:] == ["", "Login successful"]


def test_other_system_records_still_say_nothing(tmp_path: Path) -> None:
    records = system_command_records()
    stray = copy.deepcopy(records[0])
    stray["content"] = "Plain words with no tags"
    boundary = compaction_records()[1]
    assert _folded(tmp_path, [stray, boundary]) == []


# A failing command.


def test_a_commands_stderr_is_its_result_and_marks_it_failed(tmp_path: Path) -> None:
    turns = _folded(tmp_path, command_stderr_records())
    assert [(t.tool, t.text, t.result, t.failed) for t in turns] == [
        ("Ran", "/model no-such-model", "Model 'no-such-model' not found", True),
        ("Ran", "/branch", "Failed to branch conversation", True),
    ]
    assert "local-command" not in " ".join(t.result for t in turns)


def test_stderr_whose_command_is_not_on_the_page_stands_alone_failed(tmp_path: Path) -> None:
    stderr = command_stderr_records()[2]
    (turn,) = _folded(tmp_path, [stderr])
    assert (turn.tool, turn.result, turn.failed) == (
        "Output",
        "Model 'no-such-model' not found",
        True,
    )


# Rendering.


def _chat(tmp_path: Path, records: list[dict[str, JsonValue]]) -> str:
    write_transcript(tmp_path / "transcripts", WORKING_SESSION, records)
    rig = build_rig(tmp_path)
    return rig.http.get(f"/agent/{WORKING_SESSION}/chat", headers={"HX-Request": "true"}).text


def test_the_chat_draws_a_compact_once_opening_on_its_summary(tmp_path: Path) -> None:
    chat = _chat(tmp_path, compaction_records())
    assert chat.count(f'<span class="notice__text">{COMPACT}</span>') == 1
    assert 'turn--system"' not in chat
    assert f'<pre class="tool__result notice__result">{CONTINUED}' in chat
    assert COMPACTED not in chat


def test_the_chat_draws_a_lone_summary_as_the_harness(tmp_path: Path) -> None:
    chat = _chat(tmp_path, _first_run()[1:3])
    assert '<span class="sr-only">Harness:</span>' in chat
    assert f'<span class="notice__text">{SUMMARY_LINE}</span>' in chat
    assert f'<pre class="tool__result notice__result">{CONTINUED}' in chat


def test_the_chat_marks_a_failing_command(tmp_path: Path) -> None:
    chat = _chat(tmp_path, command_stderr_records())
    assert chat.count("notice--ran notice--failed") == 2
    assert "local-command" not in chat
