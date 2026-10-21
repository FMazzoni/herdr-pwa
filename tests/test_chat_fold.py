"""Tool calls fold into one quiet line between the messages; a failure is never folded away."""

import re
from pathlib import Path

from pydantic import JsonValue

from herdr_pwa.web.chat import ChatTurn, ToolRun, fold_tools
from tests.webfakes import WORKING_SESSION, assistant_says, build_rig, human_says, write_transcript


def _turn(speaker: str, n: int = 0, *, failed: bool = False) -> ChatTurn:
    return ChatTurn(
        speaker,
        f"text {n}",
        (),
        "Bash" if speaker == "tool" else "",
        "",
        failed=failed,
        truncated=False,
        call=f"tu{n}",
    )


def _shape(items: tuple[ChatTurn | ToolRun, ...]) -> list[str]:
    return [
        f"run:{','.join(t.call for t in i.tools)}" if isinstance(i, ToolRun) else i.speaker
        for i in items
    ]


def test_a_row_of_calls_is_one_run_between_the_messages() -> None:
    turns = (_turn("human"), _turn("tool", 1), _turn("tool", 2), _turn("tool", 3), _turn("agent"))
    assert _shape(fold_tools(turns)) == ["human", "run:tu1,tu2,tu3", "agent"]


def test_a_failed_call_splits_the_run_and_stands_alone() -> None:
    turns = tuple(_turn("tool", n, failed=n == 2) for n in range(5))
    assert _shape(fold_tools(turns)) == ["run:tu0,tu1", "run:tu2", "run:tu3,tu4"]


def test_a_single_call_is_a_run_of_one_that_does_not_fold() -> None:
    (run,) = fold_tools((_turn("tool", 7),))
    assert isinstance(run, ToolRun)
    assert not run.folded
    assert run.key == "tu7"


def _call(n: int, *, failed: bool = False) -> list[dict[str, JsonValue]]:
    use: dict[str, JsonValue] = {
        "type": "tool_use",
        "id": f"tu{n}",
        "name": "Bash",
        "input": {"command": f"pytest -k c{n}"},
    }
    result: dict[str, JsonValue] = {
        "type": "tool_result",
        "tool_use_id": f"tu{n}",
        "content": f"out {n}",
        "is_error": failed,
    }
    return [
        {
            "type": "assistant",
            "uuid": f"c{n}",
            "timestamp": "2026-10-10T10:00:01.000Z",
            "message": {"role": "assistant", "content": [use]},
        },
        {
            "type": "user",
            "uuid": f"r{n}",
            "timestamp": "2026-10-10T10:00:02.000Z",
            "message": {"role": "user", "content": [result]},
        },
    ]


def test_the_chat_draws_a_fold_a_failure_in_sight_and_messages_as_cards(tmp_path: Path) -> None:
    records = [
        human_says("run the tests"),
        *_call(0),
        *_call(1),
        *_call(2, failed=True),
        assistant_says("fixed"),
    ]
    write_transcript(tmp_path / "transcripts", WORKING_SESSION, records)
    rig = build_rig(tmp_path)
    chat = rig.http.get(f"/agent/{WORKING_SESSION}/chat", headers={"HX-Request": "true"}).text
    assert '<summary class="tools__head">2 tool calls</summary>' in chat
    assert chat.count('class="tools"') == 1
    # The failure is a run of its own, outside the fold, saying so on its line.
    alone = re.search(
        r'<div class="turn turn--tools" data-hp-key="tools:tu2">\s*'
        r'<span class="sr-only">Tool:</span>\s*<details class="tool tool--failed"',
        chat,
    )
    assert alone is not None
    line = chat[alone.start() :]
    assert "failed</span></summary>" in line[: line.index("</summary>") + len("</summary>")]
    assert '<article class="turn turn--human" data-hp-key="turn:u1">' in chat
    assert '<article class="turn turn--agent" data-hp-key="turn:a1">' in chat
