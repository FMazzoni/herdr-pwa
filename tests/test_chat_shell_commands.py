"""A `!` command the person ran in the harness: their line, opening on what it printed."""

from __future__ import annotations

import json
from pathlib import Path

from herdr_pwa.engine import Speaker, TranscriptTurn
from tests.catalogue import shell_command_records
from tests.engine_seam import Window, fold_turns, read_page
from tests.webfakes import WORKING_SESSION, build_rig, write_transcript


def _turns(tmp_path: Path) -> list[tuple[str, str, str, bool]]:
    path = tmp_path / "log.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in shell_command_records()))
    turns = fold_turns(read_page(path, Window(limit=400)).turns)
    return [(t.speaker.value, t.text, t.result, t.failed) for t in turns if t.speaker != "agent"]


def test_a_shell_command_is_the_persons_line_with_its_output_folded_under_it(
    tmp_path: Path,
) -> None:
    assert _turns(tmp_path) == [
        ("human", "What branch is this on?", "", False),
        ("human", "! git status --short --branch", "## main...origin/main\n M README.md", False),
        (
            "human",
            "! sudo systemctl restart example.service",
            "sudo: a terminal is required to read the password",
            True,
        ),
        ("human", "! touch notes.txt", "", False),
        ("human", "It is on main with the README changed.", "", False),
    ]


def test_a_shell_command_without_its_line_on_the_page_is_a_quiet_output(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    output = shell_command_records()[4]
    path.write_text(json.dumps(output) + "\n")
    turns = fold_turns(read_page(path, Window(limit=400)).turns)
    assert [(t.speaker, t.tool) for t in turns] == [(Speaker.notice, "Output")]


def test_the_chat_draws_it_as_yours_and_never_the_tags(tmp_path: Path) -> None:
    write_transcript(tmp_path / "transcripts", WORKING_SESSION, shell_command_records())
    chat = (
        build_rig(tmp_path)
        .http.get(f"/agent/{WORKING_SESSION}/chat", headers={"HX-Request": "true"})
        .text
    )
    assert (
        chat.count('class="turn turn--human"') + chat.count('class="turn turn--human turn--shell"')
        == 5
    )
    # Drawn once, with its prompt glyph in place of the "!" it was typed with.
    assert chat.count("git status --short --branch") == 1
    assert chat.count('class="tool turn__output"') == 1
    assert chat.count('class="tool turn__output tool--failed"') == 1
    for tag in ("bash-input", "bash-stdout", "bash-stderr"):
        assert tag not in chat
    assert "Bash completed with no output" not in chat
    assert 'class="turn turn--system"' not in chat


def _shell_chat(tmp_path: Path) -> str:
    write_transcript(tmp_path / "transcripts", WORKING_SESSION, shell_command_records())
    rig = build_rig(tmp_path)
    return rig.http.get(f"/agent/{WORKING_SESSION}/chat", headers={"HX-Request": "true"}).text


def test_a_shell_command_is_drawn_as_a_terminal_line_with_its_prompt_glyph(tmp_path: Path) -> None:
    chat = _shell_chat(tmp_path)
    assert chat.count('class="turn turn--human turn--shell"') == 3
    prompt = '<span class="turn__prompt" aria-hidden="true">$</span>'
    assert f'<p class="turn__text turn__cmd">{prompt} git status --short --branch</p>' in chat
    # A prompt to the agent is not a command, however it reads.
    assert "It is on main with the README changed." in chat
    assert chat.count("turn__cmd") == 3


def test_a_shell_command_says_no_output_only_when_the_harness_said_so(tmp_path: Path) -> None:
    turns = {t.text: t for t in _folded(tmp_path)}
    assert turns["! touch notes.txt"].outcome == "no output"
    assert turns["! git status --short --branch"].outcome == ""
    chat = _shell_chat(tmp_path / "web")
    touched = chat[chat.index("touch notes.txt") :].split("</article>")[0]
    assert '<p class="turn__clip">no output</p>' in touched
    assert chat.count(">no output<") == 1


def _folded(tmp_path: Path) -> list[TranscriptTurn]:
    path = tmp_path / "log.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in shell_command_records()))
    return list(fold_turns(read_page(path, Window(limit=400)).turns))
