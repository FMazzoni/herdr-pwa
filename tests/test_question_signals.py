"""A prompt is refused for a question only where herdr, the harness's record and the screen agree."""

import json
import logging
from pathlib import Path
from typing import NamedTuple

import pytest
from pydantic import JsonValue

from herdr_pwa.engine import AgentStatus, Source, awaits_answer
from herdr_pwa.writes.guard import (
    HELD_BOTH,
    HERDR_ALONE,
    INSISTED,
    KEYED,
    OVERRULED,
    RECORDED,
    SCREEN_ALONE,
    UNSETTLED,
    Drawn,
    QuestionCall,
    drawn_question,
    question_call,
)
from herdr_pwa.writes.prompt import PromptAsk, PromptSent, send_prompt
from herdr_pwa.writes.refusal import Refusal, Refused
from herdr_pwa.writes.resolve import Writer
from tests._fake_herd import FIXTURES, P3, S1, SETTLED, FakeHerd, Rig, capture, rig

# A dialog with keys of its own; a bare numbered run with no footer or cursor; nothing at all.
ASKING = capture("capture-blocked-permission-prompt", "visible")
ROWS = "Pick one?\n  1. Red\n  2. Blue\n"
TABLE = (FIXTURES / "claude" / "idle-box-table.txt").read_text(encoding="utf-8")
SCREENS = {Drawn.DIALOG: ASKING, Drawn.ROWS: ROWS, Drawn.NOTHING: SETTLED}


class Row(NamedTuple):
    """herdr's word, what the screen draws, the record (None: nothing says), then the call."""

    herdr: AgentStatus
    screen: Drawn
    record: bool | None
    refuse: bool
    # Whether the record had to be read, and which signal disagreed with the call, if one did.
    read: bool
    doubt: str


def _rows(herdr: AgentStatus, *calls: tuple[Drawn, bool | None, bool, bool, str]) -> list[Row]:
    return [Row(herdr, *call) for call in calls]


DIALOG, ROWED, NOTHING = Drawn.DIALOG, Drawn.ROWS, Drawn.NOTHING
ASKED = (DIALOG, ROWED)
EACH = (True, False, None)
# The whole table, every combination of the three signals.
TABLE_ROWS: list[Row] = [
    *_rows(
        AgentStatus.BLOCKED,
        *((drawn, record, True, False, "") for drawn in ASKED for record in EACH),
        (NOTHING, True, True, True, HELD_BOTH),
        (NOTHING, False, True, True, HERDR_ALONE),
        (NOTHING, None, True, True, HERDR_ALONE),
    ),
    *(
        row
        for herdr in (AgentStatus.IDLE, AgentStatus.DONE, AgentStatus.WORKING)
        for row in _rows(
            herdr,
            (DIALOG, True, True, True, OVERRULED),
            (DIALOG, False, True, True, KEYED),
            (DIALOG, None, True, True, UNSETTLED),
            (ROWED, True, True, True, OVERRULED),
            (ROWED, False, False, True, SCREEN_ALONE),
            (ROWED, None, True, True, UNSETTLED),
        )
    ),
    *(
        row
        for herdr in (AgentStatus.IDLE, AgentStatus.DONE)
        for row in _rows(
            herdr,
            (NOTHING, True, True, True, RECORDED),
            (NOTHING, False, False, True, ""),
            (NOTHING, None, False, True, ""),
        )
    ),
    # A working agent's running call is normal, so its record is not read for an empty screen.
    *_rows(AgentStatus.WORKING, *((NOTHING, record, False, False, "") for record in EACH)),
    *_rows(
        AgentStatus.UNKNOWN,
        *((drawn, record, True, False, "") for drawn in ASKED for record in EACH),
        *((NOTHING, record, False, False, "") for record in EACH),
    ),
]


def test_the_table_covers_every_combination() -> None:
    combos = {(row.herdr, row.screen, row.record) for row in TABLE_ROWS}
    assert len(combos) == len(TABLE_ROWS) == len(AgentStatus) * len(Drawn) * 3


def test_each_screen_draws_what_its_row_says() -> None:
    assert {drawn: drawn_question(screen) for drawn, screen in SCREENS.items()} == {
        drawn: drawn for drawn in SCREENS
    }
    assert drawn_question(TABLE) is Drawn.NOTHING


ROW_IDS = [f"{r.herdr}-{r.screen.value}-{r.record}" for r in TABLE_ROWS]


@pytest.mark.parametrize("row", TABLE_ROWS, ids=ROW_IDS)
def test_the_decision_table(row: Row) -> None:
    reads: list[bool] = []

    def recorded() -> bool | None:
        reads.append(True)
        return row.record

    call = question_call(SCREENS[row.screen], row.herdr, recorded)
    assert (call.refuse, bool(reads)) == (row.refuse, row.read)
    assert call.doubt == row.doubt.format(herdr=row.herdr)
    assert (call.herdr, call.screen) == (row.herdr, row.screen)


@pytest.mark.parametrize("record", [False, None])
def test_a_lone_herdr_wait_is_overridden_by_a_second_deliberate_send(record: bool | None) -> None:
    first = question_call(SETTLED, AgentStatus.BLOCKED, lambda: record)
    assert (first.refuse, first.overridable, first.doubt) == (True, True, HERDR_ALONE)
    second = question_call(SETTLED, AgentStatus.BLOCKED, lambda: record, insisted=True)
    assert (second.refuse, second.overridable, second.doubt) == (False, False, INSISTED)


@pytest.mark.parametrize("screen", [ASKING, ROWS])
def test_insisting_never_overrides_a_question_the_screen_or_record_shows(screen: str) -> None:
    for herdr in AgentStatus:
        assert question_call(screen, herdr, lambda: None, insisted=True).refuse
    held = question_call(SETTLED, AgentStatus.BLOCKED, lambda: True, insisted=True)
    assert (held.refuse, held.overridable) == (True, False)
    recorded = question_call(SETTLED, AgentStatus.IDLE, lambda: True, insisted=True)
    assert (recorded.refuse, recorded.overridable) == (True, False)


# The review's attacks: a dialog the parse recognised, above an empty-looking input box.
R = "\u2500" * 40
APPROVAL = (FIXTURES / "hermes" / "stopped-approval.txt").read_text(encoding="utf-8")
CLARIFY = (FIXTURES / "hermes" / "blocked-clarify.txt").read_text(encoding="utf-8")
CONFIRM = (FIXTURES / "hermes" / "blocked-slash-confirm.txt").read_text(encoding="utf-8")
HEREDOC = f"""\u25cf I'll write it.
{R}
 Bash command

   cat > note.md <<'EOF'
   {"\u2500" * 30}
   >
   {"\u2500" * 30}
   EOF
 Do you want to proceed?
 \u276f 1. Yes
   2. No
 Esc to cancel
"""
BARE_BOX = {
    "hermes-approval": (APPROVAL, "\u26a0 \u276f"),
    "hermes-clarify": (CLARIFY, "? \u276f"),
    "hermes-slash-confirm": (CONFIRM, "\u26a0 \u276f type 1/2/3, or use \u2191/\u2193 then Enter"),
}


@pytest.mark.parametrize("name", list(BARE_BOX))
@pytest.mark.parametrize("herdr", [AgentStatus.DONE, AgentStatus.IDLE])
def test_a_hermes_dialog_above_a_bare_prompt_still_refuses(name: str, herdr: AgentStatus) -> None:
    screen, marker = BARE_BOX[name]
    assert marker in screen, "the premise: the capture draws its prompt with a marker"
    bare = screen.replace(marker, "\u276f")
    assert drawn_question(bare) is Drawn.DIALOG
    assert question_call(bare, herdr, lambda: None).refuse


def test_a_bash_permission_whose_command_draws_a_box_still_refuses() -> None:
    assert drawn_question(HEREDOC) is Drawn.DIALOG
    assert question_call(HEREDOC, AgentStatus.IDLE, lambda: False).refuse


def test_a_call_names_every_signal_it_was_made_on() -> None:
    # The guard's decision is never the screen's alone wherever herdr has a word for the agent.
    call = question_call(ROWS, AgentStatus.IDLE, lambda: False)
    expected = QuestionCall(
        refuse=False, herdr=AgentStatus.IDLE, screen=Drawn.ROWS, record=False, doubt=call.doubt
    )
    assert call == expected
    assert "herdr" in call.doubt
    assert "record" in call.doubt


@pytest.mark.parametrize("screen", [ROWS, SETTLED, TABLE])
def test_the_screen_alone_never_decides_where_herdr_has_a_word(screen: str) -> None:
    calls = {
        question_call(screen, herdr, lambda record=record: record).refuse
        for herdr in (AgentStatus.BLOCKED, AgentStatus.IDLE)
        for record in (True, False)
    }
    assert calls == {True, False}, "the same screen, read with other signals, is called both ways"


# The harness's record: a Claude Code log with a call still unanswered, or not


def _call(call: str) -> dict[str, JsonValue]:
    return {
        "type": "assistant",
        "uuid": f"use-{call}",
        "timestamp": "2026-08-19T00:00:00Z",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": call, "name": "Bash", "input": {}}],
        },
    }


def _result(call: str) -> dict[str, JsonValue]:
    return {
        "type": "user",
        "uuid": f"res-{call}",
        "timestamp": "2026-08-19T00:00:01Z",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": call, "content": "ok"}],
        },
    }


def _prompt(text: str) -> dict[str, JsonValue]:
    return {
        "type": "user",
        "uuid": f"said-{text}",
        "timestamp": "2026-08-19T00:00:02Z",
        "promptSource": "typed",
        "message": {"role": "user", "content": text},
    }


def _store(root: Path, *records: dict[str, JsonValue]) -> Path:
    folder = root / "-home-user-scratch"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{S1}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    return root


@pytest.mark.parametrize(
    ("records", "pending"),
    [
        ((_call("a"),), True),
        ((_call("a"), _result("a")), False),
        ((_call("a"), _call("b"), _result("a")), True),
        ((_call("a"), _prompt("go on")), False),
        ((_prompt("hello"),), False),
    ],
)
def test_a_claude_log_says_whether_a_call_is_unanswered(
    tmp_path: Path, records: tuple[dict[str, JsonValue], ...], pending: bool
) -> None:
    root = _store(tmp_path, *records)
    assert awaits_answer(Source.of(S1, agent_kind="claude", root=root)) is pending


def test_no_log_says_nothing(tmp_path: Path) -> None:
    assert awaits_answer(Source.of(S1, agent_kind="claude", root=tmp_path)) is None
    assert awaits_answer(Source.of("", agent_kind="claude", root=tmp_path)) is None


@pytest.mark.parametrize("kind", ["hermes", "opencode", "codex", None])
def test_a_harness_whose_store_is_not_known_to_hold_calls_says_nothing(
    tmp_path: Path, kind: str | None
) -> None:
    root = _store(tmp_path, _call("a"))
    assert awaits_answer(Source.of(S1, agent_kind=kind, root=root)) is None


# Through the prompt write


def _herd(screen: str, status: str) -> FakeHerd:
    herd = FakeHerd()
    herd.put(P3, S1, status=status)
    herd.screens[P3] = [screen]
    return herd


def _rig(tmp_path: Path, herd: FakeHerd, *records: dict[str, JsonValue]) -> Rig:
    made = rig(tmp_path, herd)
    root = _store(tmp_path / "projects", *records)
    w = made.writer
    writer = Writer(w.client, w.audit, w.pacing, stores=(str(root),), memory=w.memory)
    return Rig(made.herd, writer, made.clock, made.state)


def test_the_reported_screen_with_herdr_idle_and_no_call_waiting_is_sent(tmp_path: Path) -> None:
    r = _rig(tmp_path, _herd(TABLE, "idle"), _call("a"), _result("a"))
    result = send_prompt(r.writer, PromptAsk(S1, "the hygiene tab"))
    assert isinstance(result, PromptSent), result
    assert result.doubt == "", "every signal agrees: nothing asks"


def test_a_screen_only_question_is_sent_and_says_who_disagreed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    r = _rig(tmp_path, _herd(ROWS, "idle"), _call("a"), _result("a"))
    with caplog.at_level(logging.WARNING, logger="herdr_pwa.writes.prompt"):
        result = send_prompt(r.writer, PromptAsk(S1, "carry on"))
    assert isinstance(result, PromptSent), result
    assert result.doubt == SCREEN_ALONE.format(herdr=AgentStatus.IDLE)
    (said,) = [rec.getMessage() for rec in caplog.records if "disagree" in rec.getMessage()]
    assert "herdr=idle screen=rows record=False refused=False" in said


def test_a_dialog_with_keys_of_its_own_holds_though_no_call_waits(tmp_path: Path) -> None:
    # A picker or a trust prompt Claude Code draws itself writes no tool call to the log.
    r = _rig(tmp_path, _herd(ASKING, "idle"), _call("a"), _result("a"))
    result = send_prompt(r.writer, PromptAsk(S1, "carry on"))
    assert isinstance(result, Refused), result
    assert result.reason == KEYED.format(herdr=AgentStatus.IDLE)


def test_the_screen_and_the_record_outvote_herdr(tmp_path: Path) -> None:
    r = _rig(tmp_path, _herd(ROWS, "idle"), _call("a"))
    result = send_prompt(r.writer, PromptAsk(S1, "carry on"))
    assert isinstance(result, Refused), result
    assert result.refusal is Refusal.QUESTION_SHOWING
    assert result.reason == OVERRULED.format(herdr=AgentStatus.IDLE)


def test_herdr_waiting_with_no_question_drawn_still_refuses_and_says_so(tmp_path: Path) -> None:
    r = _rig(tmp_path, _herd(SETTLED, "blocked"))
    result = send_prompt(r.writer, PromptAsk(S1, "carry on"))
    assert isinstance(result, Refused), result
    assert (result.refusal, result.reason) == (Refusal.QUESTION_SHOWING, HERDR_ALONE)


def test_agreeing_signals_refuse_without_a_doubt(tmp_path: Path) -> None:
    r = _rig(tmp_path, _herd(ASKING, "blocked"), _call("a"))
    result = send_prompt(r.writer, PromptAsk(S1, "carry on"))
    assert isinstance(result, Refused), result
    assert (result.refusal, result.reason) == (Refusal.QUESTION_SHOWING, "")


# Every real question capture refuses when its signals agree: herdr blocked, a call unanswered.
BLOCKED = sorted(p.name for p in FIXTURES.glob("capture-blocked-*") if p.is_dir())


@pytest.mark.parametrize("name", BLOCKED)
def test_every_question_capture_refuses_when_its_signals_agree(name: str) -> None:
    call = question_call(capture(name, "visible"), AgentStatus.BLOCKED, lambda: True)
    assert (call.refuse, call.doubt) == (True, "")
    # And with herdr's word lagging behind, the record and the screen still hold it.
    assert question_call(capture(name, "visible"), AgentStatus.IDLE, lambda: True).refuse
