"""A slash command the person ran is one quiet line naming it, opening on what it printed."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from pydantic import JsonValue

from herdr_pwa.engine import Speaker, TranscriptTurn, is_api_error, is_slash_command
from tests._jsonpath import obj
from tests.catalogue import api_error_records, slash_command_records
from tests.engine_seam import Window, fold_turns, is_message, read_page
from tests.webfakes import WORKING_SESSION, assistant_says, build_rig, write_transcript

COMPACT = "/compact recall what we have built so far and keep any useful tooling"
COMPACTED = "Compacted (ctrl+o to see full summary)"
MODEL_SET = "Set model to `Opus 5.5 (default)` and saved as your default for new sessions"
COPIED = (
    "Copied to clipboard (1782 characters, 51 lines)\nAlso written to /var/tmp/agent/copy.jsonc"
)


def _content(record: dict[str, JsonValue]) -> str:
    content = obj(record, "message").get("content") if record.get("type") == "user" else None
    return content if isinstance(content, str) else str(record.get("content", ""))


def _turns(tmp_path: Path, records: list[dict[str, JsonValue]]) -> list[TranscriptTurn]:
    path = tmp_path / "log.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return fold_turns(read_page(path, Window(limit=400)).turns)


def _commands(tmp_path: Path) -> list[TranscriptTurn]:
    return [t for t in _turns(tmp_path, slash_command_records()) if is_slash_command(t)]


def _with_content(record: dict[str, JsonValue], content: str) -> dict[str, JsonValue]:
    edited = copy.deepcopy(record)
    obj(edited, "message")["content"] = content
    return edited


# The fixture: real shapes, scrubbed, in the order Claude Code writes them.


def test_the_fixture_holds_each_command_shape_found_locally() -> None:
    records = slash_command_records()
    said = [_content(r) for r in records]
    names = [s.split("</command-name>")[0] for s in said if s.startswith("<command-name>")]
    assert names == [f"<command-name>/{n}" for n in ("compact", "model", "chrome", "copy", "clear")]
    # Measured: the output is a record of its own, and an empty /clear writes it as a system one.
    outputs = [r for r in records if _content(r).startswith("<local-command-stdout>")]
    assert [str(r.get("type")) for r in outputs] == ["user"] * 4 + ["system"]
    assert "\x1b[2m" in _content(outputs[0])
    assert sum(r.get("isMeta") is True for r in records) == 5


# Reading: the command, quietly, never its tags.


def test_no_tag_reaches_the_chat(tmp_path: Path) -> None:
    turns = _turns(tmp_path, slash_command_records())
    shown = " ".join(t.text + t.result for t in turns)
    assert "<command-" not in shown
    assert "local-command" not in shown
    assert not any(t.speaker is Speaker.system for t in turns)


def test_each_command_is_one_line_naming_it_and_its_arguments(tmp_path: Path) -> None:
    turns = _commands(tmp_path)
    assert [(t.speaker, t.tool, t.text) for t in turns] == [
        (Speaker.notice, "Ran", COMPACT),
        (Speaker.notice, "Ran", "/model"),
        (Speaker.notice, "Ran", "/chrome"),
        (Speaker.notice, "Ran", "/copy"),
        (Speaker.notice, "Ran", "/clear"),
    ]
    assert not any(t.failed for t in turns)


def test_what_a_command_printed_is_its_result_with_the_terminal_styling_dropped(
    tmp_path: Path,
) -> None:
    turns = _commands(tmp_path)
    # "(no content)" is the harness saying there was none; /clear's output was never a user record.
    assert [t.result for t in turns] == [COMPACTED, MODEL_SET, "", COPIED, ""]


def test_the_line_keeps_the_commands_own_id(tmp_path: Path) -> None:
    records = slash_command_records()
    turns = _commands(tmp_path)
    command_ids = [str(r.get("uuid")) for r in records if _content(r).startswith("<command-name>")]
    assert [t.uuid for t in turns] == command_ids


def test_output_whose_command_is_not_on_the_page_stands_alone(tmp_path: Path) -> None:
    records = slash_command_records()
    stdout = next(r for r in records if COMPACTED[:9] in _content(r))
    (turn,) = _turns(tmp_path, [stdout])
    assert (turn.speaker, turn.tool, turn.text, turn.result) == (
        Speaker.notice,
        "Output",
        COMPACTED,
        COMPACTED,
    )
    assert is_slash_command(turn)


def test_a_commands_arguments_are_decoded_once(tmp_path: Path) -> None:
    record = next(r for r in slash_command_records() if "/compact" in _content(r))
    edited = _with_content(record, _content(record).replace("recall what", "&lt;b&gt; &amp; what"))
    (turn,) = _turns(tmp_path, [edited])
    assert turn.text == "/compact <b> & what we have built so far and keep any useful tooling"


def test_a_typed_prompt_that_quotes_the_tags_is_still_the_persons(tmp_path: Path) -> None:
    human = next(r for r in slash_command_records() if r.get("promptSource") == "typed")
    typed = _with_content(human, "<command-name>/compact</command-name> shows up raw, why?")
    (turn,) = _turns(tmp_path, [typed])
    assert turn.speaker is Speaker.human
    assert turn.text.startswith("<command-name>")


# Paging: a command is something done, not something said.


def test_a_command_is_not_a_message(tmp_path: Path) -> None:
    turns = _commands(tmp_path)
    assert len(turns) == 5
    assert not any(is_message(t) for t in turns)


def test_a_page_of_messages_reaches_past_commands_to_what_was_said(tmp_path: Path) -> None:
    records = [*slash_command_records(), assistant_says("Carrying on.", uuid="after")]
    path = tmp_path / "log.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    page = read_page(path, Window(limit=400, messages=2))
    said = [t for t in page.turns if is_message(t)]
    assert [t.speaker for t in said] == [Speaker.human, Speaker.agent]
    assert sum(is_slash_command(t) for t in fold_turns(page.turns)) == 5


def test_a_command_between_two_refusals_ends_the_run(tmp_path: Path) -> None:
    refused = next(r for r in api_error_records() if r.get("error") == "invalid_request")
    again = copy.deepcopy(refused)
    again["uuid"] = "refused-again"
    compact = next(r for r in slash_command_records() if "/compact" in _content(r))
    turns = _turns(tmp_path, [refused, compact, again])
    # The person acted between them: the second refusal answers that, not a retry of the first.
    assert [t.times for t in turns if is_api_error(t)] == [1, 1]


def test_output_is_not_folded_across_an_api_error(tmp_path: Path) -> None:
    records = slash_command_records()
    compact = next(r for r in records if "/compact" in _content(r))
    stdout = next(r for r in records if COMPACTED[:9] in _content(r))
    refused = next(r for r in api_error_records() if r.get("error") == "invalid_request")
    turns = _turns(tmp_path, [compact, refused, stdout])
    assert [(t.tool, t.result) for t in turns] == [
        ("Ran", ""),
        ("Context", "Prompt is too long"),
        ("Output", COMPACTED),
    ]


# Rendering: a quiet line, escaped once, opening on the output when there is one.


def _chat(tmp_path: Path, records: list[dict[str, JsonValue]]) -> str:
    write_transcript(tmp_path / "transcripts", WORKING_SESSION, records)
    rig = build_rig(tmp_path)
    return rig.http.get(f"/agent/{WORKING_SESSION}/chat", headers={"HX-Request": "true"}).text


def test_the_chat_draws_a_command_as_a_line_that_opens_on_its_output(tmp_path: Path) -> None:
    chat = _chat(tmp_path, slash_command_records())
    assert "command-name" not in chat
    assert "local-command" not in chat
    assert (
        f'<span class="tool__name notice__label">Ran</span> <span class="notice__text">{COMPACT}'
    ) in chat
    assert f'<pre class="tool__result notice__result">{COMPACTED}</pre>' in chat
    assert '<span class="sr-only">Slash command:</span>' in chat
    assert '<span class="sr-only">Task:</span>' not in chat
    assert "notice--failed" not in chat


def test_a_command_that_printed_nothing_is_a_still_line(tmp_path: Path) -> None:
    clear = next(r for r in slash_command_records() if "/clear" in _content(r))
    chat = _chat(tmp_path, [clear])
    assert 'class="tool notice notice--ran notice--still"' in chat
    assert "<details" not in chat.split('turn--notice"', 1)[1].split("</div>", 1)[0]


def test_markup_in_a_commands_arguments_is_drawn_as_text(tmp_path: Path) -> None:
    record = next(r for r in slash_command_records() if "/compact" in _content(r))
    edited = _with_content(
        record, _content(record).replace("recall what", "&lt;b&gt;bold&lt;/b&gt;")
    )
    chat = _chat(tmp_path, [edited])
    assert "/compact &lt;b&gt;bold&lt;/b&gt; we have built" in chat
    assert "<b>bold</b>" not in chat
