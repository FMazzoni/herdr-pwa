"""A `!` command the person ran in the harness: their line, opening on what it printed."""

from __future__ import annotations

import json
from pathlib import Path

from herdr_pwa.engine import Speaker
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
    assert chat.count('class="turn turn--human"') == 5
    assert chat.count("! git status --short --branch") == 1
    assert chat.count('class="tool turn__output"') == 1
    assert chat.count('class="tool turn__output tool--failed"') == 1
    for tag in ("bash-input", "bash-stdout", "bash-stderr"):
        assert tag not in chat
    assert "Bash completed with no output" not in chat
    assert 'class="turn turn--system"' not in chat
