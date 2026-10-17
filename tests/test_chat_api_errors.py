"""A turn the API refused is the harness's quiet line saying what unblocks it, never the agent's words."""

from __future__ import annotations

import copy
import json
from collections import Counter
from pathlib import Path

from pydantic import JsonValue

from herdr_pwa.engine import Speaker, TranscriptTurn, is_api_error
from tests._jsonpath import obj
from tests.catalogue import api_error_records, assistant_says
from tests.engine_seam import Window, fold_turns, is_message, read_page, tally_claude
from tests.test_opencode import ERROR_ROWS, ERRORED, build
from tests.webfakes import (
    SNAPSHOT,
    WORKING_PANE,
    WORKING_SESSION,
    agents_of,
    build_rig,
    snapshot_frame,
    write_transcript,
)

CONTEXT_LINE = "Full · run /compact or start a new session"
SESSION_LINE = "Session limit · resets 2:30am (UTC)"


def _write(tmp_path: Path, records: list[dict[str, JsonValue]]) -> Path:
    path = tmp_path / "log.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


def _turns(tmp_path: Path, records: list[dict[str, JsonValue]]) -> list[TranscriptTurn]:
    return read_page(_write(tmp_path, records), Window(limit=400)).turns


def _errors() -> list[dict[str, JsonValue]]:
    return [r for r in api_error_records() if r.get("isApiErrorMessage") is True]


def _said(record: dict[str, JsonValue]) -> str:
    content = obj(record, "message").get("content")
    assert isinstance(content, list)
    block = content[0]
    assert isinstance(block, dict)
    return str(block.get("text"))


# The fixture: real shapes, the order a full context was measured in.


def test_the_fixture_holds_every_api_error_shape_found_locally() -> None:
    errors = _errors()
    codes = Counter(str(r.get("error")) for r in errors)
    assert codes == {
        "invalid_request": 5,
        "rate_limit": 4,
        "server_error": 1,
        "authentication_failed": 1,
        "model_not_found": 1,
    }
    # Measured: Claude Code writes them as an answer from a model that is not one.
    assert {str(obj(r, "message").get("model")) for r in errors} == {"<synthetic>"}
    assert {str(r.get("type")) for r in errors} == {"assistant"}


# Reading: one quiet line each, saying what happened and what unblocks it.


def test_no_api_error_reads_as_the_agent_speaking(tmp_path: Path) -> None:
    turns = _turns(tmp_path, _errors())
    assert len(turns) == len(_errors())
    assert all(t.speaker is Speaker.notice and is_api_error(t) for t in turns)
    assert all(t.failed for t in turns)
    assert not any(is_message(t) for t in turns)


def test_a_full_context_says_to_compact_and_keeps_the_harness_words(tmp_path: Path) -> None:
    record = next(r for r in _errors() if r.get("error") == "invalid_request")
    (turn,) = _turns(tmp_path, [record])
    assert (turn.tool, turn.text, turn.result) == ("Context", CONTEXT_LINE, "Prompt is too long")


def test_a_usage_limit_keeps_its_reset_time(tmp_path: Path) -> None:
    limits = [r for r in _errors() if r.get("error") == "rate_limit"]
    turns = _turns(tmp_path, limits)
    assert {(t.tool, t.text) for t in turns} == {
        ("Limit", SESSION_LINE),
        ("Limit", "Weekly limit · resets Oct 5, 3am (UTC)"),
    }
    # Opened, the line shows exactly what the harness wrote.
    assert {t.result for t in turns} == {_said(r) for r in limits}


def test_any_other_api_error_is_its_own_text_with_nothing_more_to_open(tmp_path: Path) -> None:
    others = [r for r in _errors() if r.get("error") not in ("invalid_request", "rate_limit")]
    turns = _turns(tmp_path, others)
    assert [t.tool for t in turns] == ["Error"] * 3
    assert [t.text for t in turns] == [_said(r) for r in others]
    assert [t.result for t in turns] == [""] * 3
    assert "Login expired · Please run /login" in [t.text for t in turns]


def test_the_usage_tally_still_leaves_them_out(tmp_path: Path) -> None:
    tally = tally_claude(_write(tmp_path, _errors())).tally
    assert tally.messages == 0
    assert tally.models == []


# Folding: a run of one error is one line with a count.


def _folded(tmp_path: Path) -> list[TranscriptTurn]:
    return fold_turns(_turns(tmp_path, api_error_records()))


def test_retries_between_notices_and_peers_fold_into_one_line_where_the_last_stands(
    tmp_path: Path,
) -> None:
    turns = _folded(tmp_path)
    kinds = [(t.speaker, t.tool) for t in turns]
    # Four refusals, each set off by a notice or a peer, then the prompt that ended the run.
    first = kinds.index((Speaker.notice, "Context"))
    assert turns[first].times == 4
    assert [k[0] for k in kinds[first - 3 : first]] == [
        Speaker.notice,
        Speaker.peer,
        Speaker.notice,
    ]
    assert turns[first + 1].speaker is Speaker.human
    # The line keeps the newest refusal's id and frontier, so a later poll extends it in place.
    raw = _turns(tmp_path, api_error_records())
    newest = [t for t in raw if t.tool == "Context"][3]
    assert (turns[first].uuid, turns[first].at) == (newest.uuid, newest.at)


def test_what_the_person_or_a_harness_note_says_starts_a_new_run(tmp_path: Path) -> None:
    turns = _folded(tmp_path)
    contexts = [t for t in turns if t.tool == "Context"]
    # The prompt after the fourth refusal was refused once more, then /compact ran.
    assert [t.times for t in contexts] == [4, 1]
    sessions = [t for t in turns if t.text == SESSION_LINE]
    assert [t.times for t in sessions] == [3]
    # A different error is a different line, whatever lies between.
    assert sum(t.tool == "Error" for t in turns) == 3


def test_a_tool_call_between_two_errors_ends_the_run(tmp_path: Path) -> None:
    error = next(r for r in _errors() if r.get("error") == "invalid_request")
    again = copy.deepcopy(error)
    again["uuid"] = "refused-again"
    call: dict[str, JsonValue] = {
        "type": "assistant",
        "uuid": "a-call",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}}],
        },
    }
    turns = fold_turns(_turns(tmp_path, [error, call, again]))
    assert [t.times for t in turns if is_api_error(t)] == [1, 1]


def test_a_page_of_messages_reaches_past_errors_to_what_was_said(tmp_path: Path) -> None:
    records = [*api_error_records(), assistant_says("Compacted; carrying on.", uuid="after")]
    page = read_page(_write(tmp_path, records), Window(limit=400, messages=2))
    said = [t for t in page.turns if is_message(t)]
    # The newest two messages are the last prompt and the agent's line, eight errors apart.
    assert [t.speaker for t in said] == [Speaker.human, Speaker.agent]
    assert sum(is_api_error(t) for t in page.turns) == 8


# Rendering: a quiet line, never an agent bubble.


def _chat(tmp_path: Path, records: list[dict[str, JsonValue]]) -> str:
    write_transcript(tmp_path / "transcripts", WORKING_SESSION, records)
    rig = build_rig(tmp_path)
    return rig.http.get(f"/agent/{WORKING_SESSION}/chat", headers={"HX-Request": "true"}).text


def test_the_chat_draws_a_full_context_as_one_counted_line(tmp_path: Path) -> None:
    chat = _chat(tmp_path, api_error_records())
    assert (
        '<span class="tool__name notice__label">Context</span> '
        f'<span class="notice__text">{CONTEXT_LINE}</span>'
    ) in chat
    assert '<span class="notice__times"><span aria-hidden="true">&middot; </span>4 times' in chat
    assert '<span class="sr-only">Harness:</span>' in chat
    # The harness's words are behind the line, never in an agent bubble.
    assert '<pre class="tool__result notice__result">Prompt is too long</pre>' in chat
    assert chat.count("Prompt is too long") == 2
    assert chat.count('class="turn turn--agent"') == 1


def test_an_error_with_nothing_to_open_is_a_still_line(tmp_path: Path) -> None:
    login = next(r for r in _errors() if r.get("error") == "authentication_failed")
    chat = _chat(tmp_path, [login])
    assert 'class="tool notice notice--error notice--failed notice--still"' in chat
    assert "<details" not in chat.split('turn--notice"', 1)[1].split("</div>", 1)[0]
    assert "Login expired · Please run /login" in chat


# opencode: the same lines, out of its own store.


def _opencode_chat(tmp_path: Path) -> str:
    frame = snapshot_frame()
    agent = next(a for a in agents_of(frame) if a.get("pane_id") == WORKING_PANE)
    agent["agent"] = "opencode"
    agent["agent_session"] = {
        "agent": "opencode",
        "kind": "id",
        "source": "herdr:opencode",
        "value": ERRORED,
    }
    agent["cwd"] = agent["foreground_cwd"] = "/home/user/project"
    build(tmp_path / "transcripts" / "opencode.db", ERROR_ROWS)
    rig = build_rig(tmp_path, {SNAPSHOT: frame})
    return rig.http.get(f"/agent/{ERRORED}/chat", headers={"HX-Request": "true"}).text


def test_an_opencode_interrupt_is_a_still_line_that_never_reads_as_failed(tmp_path: Path) -> None:
    chat = _opencode_chat(tmp_path)
    still = 'class="tool notice notice--interrupted notice--still"'
    assert chat.count(still) == 3
    assert "notice--interrupted notice--failed" not in chat
    # Said by its label alone: nothing after it, nothing to open.
    assert '<span class="tool__name notice__label">Interrupted</span></p>' in chat


def test_an_opencode_rate_limit_opens_on_the_providers_words(tmp_path: Path) -> None:
    chat = _opencode_chat(tmp_path)
    assert (
        '<span class="tool__name notice__label">Limit</span> '
        '<span class="notice__text">Rate limited · wait, then send it again</span>'
    ) in chat
    assert '<pre class="tool__result notice__result">Too many requests in a short window.' in chat
    assert chat.count('class="tool notice notice--context notice--failed"') == 1
