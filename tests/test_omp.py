"""omp's session log as the Chat tab's conversation, and its Ask dialog as the answer menu."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Final, NamedTuple

import pytest

from herdr_pwa.decisions import decide_screen
from herdr_pwa.engine import (
    Agent,
    BlockedPrompt,
    HistoryState,
    MenuShape,
    ModelRef,
    PromptSource,
    Source,
    Speaker,
    as_pane_id,
    as_session_id,
    harness_kinds,
    harness_of,
    prompt_source,
    prompt_stamp,
    session_origin,
)
from herdr_pwa.writes.answer import KeyAsk, KeySent, send_key
from herdr_pwa.writes.keys import allowed_keys, control_token
from herdr_pwa.writes.outcome import Outcome
from herdr_pwa.writes.refusal import Refusal, Refused
from tests._fake_herd import P3, FakeHerd, rig
from tests.engine_seam import (
    MAX_PAGE_BYTES,
    OmpAsk,
    Window,
    adapter_for,
    history,
    pending_ask,
    pending_question,
    usage,
)
from tests.webfakes import (
    READ,
    SNAPSHOT,
    WORKING_PANE,
    Rig,
    agents_of,
    build_rig,
    read_frame,
    snapshot_frame,
)

FIXTURES = Path(__file__).parent / "fixtures"
OMP = FIXTURES / "omp"
TRANSCRIPTS = FIXTURES / "transcript" / "omp"
SESSION = "2026-10-23T17-10-53-588Z_5e550030-0000-4000-8000-000000000000"
TRANSCRIPT = TRANSCRIPTS / f"{SESSION}.jsonl"
PENDING = "2026-10-23T17-10-53-588Z_5e550031-0000-4000-8000-000000000000"
PENDING_TRANSCRIPT = TRANSCRIPTS / f"{PENDING}.jsonl"
PREVIEW_PENDING = "2026-10-23T17-10-53-588Z_5e550032-0000-4000-8000-000000000000"


def _path(session: str) -> str:
    """The session path an omp extension reports for *session*."""
    return f"/home/user/.omp/agent/sessions/-home-user-project/{session}.jsonl"


def _parse(text: str, ask: OmpAsk | None = None, *, kind: str = "omp") -> BlockedPrompt:
    """Parse an omp screen with the question its record holds; without one, it draws no menu."""
    question = None if ask is None else ask.recorded
    return BlockedPrompt.parse(text, source=PromptSource(agent_kind=kind, question=question))


def _omp_root(tmp_path: Path) -> Path:
    """The fixture laid out the way omp lays a real tree out: one project folder, one log."""
    project = tmp_path / "-home-user-scratch-omp-ask"
    project.mkdir()
    (project / f"{SESSION}.jsonl").write_bytes(TRANSCRIPT.read_bytes())
    return tmp_path


def _source(tmp_path: Path) -> Source:
    return Source.of(SESSION, agent_kind="omp", root=_omp_root(tmp_path))


def _agent(session: str = SESSION) -> Agent:
    """An omp agent whose pane reported *session* as its file, as the extension reports it."""
    return Agent(
        session=as_session_id(session),
        target=as_pane_id(WORKING_PANE),
        agent="omp",
    )


# The adapter


def test_omp_is_a_kind_the_chat_tab_can_answer_for() -> None:
    """Being absent from either of these is what would leave the Chat tab silent."""
    assert "omp" in harness_kinds()
    assert adapter_for("omp") is not None
    assert harness_of("omp") is not None


def test_the_page_is_a_conversation_not_a_screen(tmp_path: Path) -> None:
    page = history(_source(tmp_path), Window(limit=100))
    speakers = {turn.speaker for turn in page.turns}
    assert Speaker.human in speakers
    assert Speaker.agent in speakers
    assert Speaker.tool in speakers
    assert page.state is HistoryState.ok
    assert not page.has_more


def test_the_prompts_are_the_two_that_were_typed(tmp_path: Path) -> None:
    page = history(_source(tmp_path), Window(limit=100))
    typed = [turn.text for turn in page.turns if turn.speaker is Speaker.human]
    assert typed == [
        (
            "Use your ask tool right away to ask me which fruit to pick, with exactly three "
            "options: Apple, Banana, Cherry, each with a one-line description. Do nothing else."
        ),
        (
            "Now use your ask tool again: ask which deploy plan to use, with three options "
            "(Canary, Blue-green, Big bang). Give each option a one-line description AND a "
            "multi-line preview of 3 lines showing its steps. Do nothing else."
        ),
    ]


def test_a_call_is_named_by_omp_intent_and_its_result_folds_on(tmp_path: Path) -> None:
    page = history(_source(tmp_path), Window(limit=100))
    calls = [turn for turn in page.turns if turn.speaker is Speaker.tool and turn.tool != "result"]
    assert [turn.tool for turn in calls] == ["ask", "ask"]
    assert [turn.text for turn in calls] == [
        "Asking which fruit to pick",
        "Asking which deploy plan to use",
    ]
    first, second = calls
    assert first.call == "call_00000001"
    assert first.result == "User selected: Apple"
    assert not first.failed
    assert second.result == "Ask tool was cancelled by the user"
    assert second.failed


def test_thinking_and_bookkeeping_never_reach_the_chat(tmp_path: Path) -> None:
    page = history(_source(tmp_path), Window(limit=100))
    text = " ".join(turn.text for turn in page.turns)
    assert "The user wants me to use the ask tool" not in text
    assert "system-prompt-digest" not in text
    assert "resolvedModelIsFallback" not in text
    assert "dispose" not in text


def test_every_turn_has_a_stable_id_to_append_by(tmp_path: Path) -> None:
    page = history(_source(tmp_path), Window(limit=100))
    uuids = [turn.uuid for turn in page.turns]
    assert all(uuids)
    assert len(uuids) == len(set(uuids))


# Paging, by the adapter's own byte frontier


def test_paging_upward_walks_the_whole_file_without_repeating(tmp_path: Path) -> None:
    source = _source(tmp_path)
    whole = history(source, Window(limit=100))
    collected: list[str] = []
    guard = 0
    before: int | None = None
    while guard < 50:
        guard += 1
        page = history(source, Window(limit=2, before=before))
        if not page.turns and not page.has_more:
            break
        collected = [turn.uuid for turn in page.turns] + collected
        if not page.has_more:
            break
        before = page.cursor
    assert collected == [turn.uuid for turn in whole.turns]


def test_a_session_name_is_matched_exactly_not_as_a_glob(tmp_path: Path) -> None:
    """A session id arrives off a URL segment; ``*`` must not match some other session's log."""
    root = _omp_root(tmp_path)
    (root / "-home-user-scratch-omp-ask" / "other.jsonl").write_text("{}\n")
    assert history(Source.of("*", agent_kind="omp", root=root)).state is HistoryState.absent


def test_since_returns_only_what_follows_the_frontier(tmp_path: Path) -> None:
    source = _source(tmp_path)
    whole = history(source, Window(limit=100))
    first_message = next(i for i, turn in enumerate(whole.turns) if turn.speaker is Speaker.human)
    frontier = whole.turns[first_message].at
    page = history(source, Window(limit=100, after=frontier))
    # `after=at` starts with the turn at that frontier, as TranscriptTurn.at promises.
    assert [turn.uuid for turn in page.turns] == [
        turn.uuid for turn in whole.turns if turn.at >= frontier
    ]


def test_after_at_starts_with_the_turn_at_the_frontier(tmp_path: Path) -> None:
    """TranscriptTurn.at promises ``after=at`` starts with it; a dropped one never loads again."""
    source = _source(tmp_path)
    whole = history(source, Window(limit=100))
    third = whole.turns[2]
    page = history(source, Window(limit=100, after=third.at))
    assert page.turns[0].uuid == third.uuid


def _batched_log() -> bytes:
    """One assistant record holding prose and two calls, as omp may write when it batches."""
    records = [
        {"type": "session", "id": "s", "cwd": "/home/user/p"},
        {
            "type": "message",
            "id": "u1",
            "message": {"role": "user", "content": [{"type": "text", "text": "go"}]},
        },
        {
            "type": "message",
            "id": "a1",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "first prose"},
                    {"type": "toolCall", "id": "c1", "name": "read", "arguments": {"path": "/a"}},
                    {"type": "toolCall", "id": "c2", "name": "read", "arguments": {"path": "/b"}},
                ],
            },
        },
    ]
    return ("\n".join(json.dumps(record) for record in records) + "\n").encode()


def _write_log(tmp_path: Path, data: bytes) -> Source:
    project = tmp_path / "-home-user-batched"
    project.mkdir()
    (project / f"{SESSION}.jsonl").write_bytes(data)
    return Source.of(SESSION, agent_kind="omp", root=tmp_path)


def test_a_batched_record_keeps_its_order_and_its_turns(tmp_path: Path) -> None:
    """Prose then calls, oldest first, even though the page is gathered newest first."""
    source = _write_log(tmp_path, _batched_log())
    page = history(source, Window(limit=100))
    assert [turn.uuid for turn in page.turns] == ["u1", "a1:0", "a1:1", "a1:2"]


def test_a_batched_record_is_never_split_by_a_limit(tmp_path: Path) -> None:
    """A page that fills partway through a record still carries the whole record."""
    source = _write_log(tmp_path, _batched_log())
    page = history(source, Window(limit=2))
    older = history(source, Window(limit=100, before=page.cursor))
    got = [turn.uuid for turn in older.turns] + [turn.uuid for turn in page.turns]
    assert got == ["u1", "a1:0", "a1:1", "a1:2"]
    assert [turn.uuid for turn in page.turns] == ["a1:0", "a1:1", "a1:2"]


def test_a_record_over_the_page_budget_is_not_lost(tmp_path: Path) -> None:
    """A record longer than one page's bytes still pages in, not silently skipped."""
    records = [
        {"type": "session", "id": "s", "cwd": "/home/user/p"},
        {
            "type": "message",
            "id": "u1",
            "message": {"role": "user", "content": [{"type": "text", "text": "go"}]},
        },
        {
            "type": "message",
            "id": "r1",
            "message": {
                "role": "toolResult",
                "toolCallId": "c1",
                "content": [{"type": "text", "text": "x" * (MAX_PAGE_BYTES + 10)}],
            },
        },
        {
            "type": "message",
            "id": "a1",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
        },
    ]
    data = ("\n".join(json.dumps(record) for record in records) + "\n").encode()
    source = _write_log(tmp_path, data)
    found = [turn.uuid for turn in history(source, Window(limit=10)).turns]
    assert found == ["u1", "r1", "a1"]


def test_a_malformed_block_does_not_drop_the_record(tmp_path: Path) -> None:
    records = [
        {"type": "session", "id": "s", "cwd": "/home/user/p"},
        {
            "type": "message",
            "id": "a1",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "ok"},
                    7,
                    {"type": "toolCall", "id": "c1", "name": "read", "arguments": {"path": "/a"}},
                ],
            },
        },
    ]
    data = ("\n".join(json.dumps(record) for record in records) + "\n").encode()
    page = history(_write_log(tmp_path, data), Window(limit=10))
    assert [turn.text for turn in page.turns] == ["ok", "/a"]


def test_a_refused_answer_is_a_notice(tmp_path: Path) -> None:
    records = [
        {"type": "session", "id": "s", "cwd": "/home/user/p"},
        {
            "type": "message",
            "id": "u1",
            "message": {"role": "user", "content": [{"type": "text", "text": "go"}]},
        },
        {
            "type": "message",
            "id": "a1",
            "message": {
                "role": "assistant",
                "stopReason": "error",
                "errorMessage": "provider overloaded",
            },
        },
    ]
    data = ("\n".join(json.dumps(record) for record in records) + "\n").encode()
    page = history(_write_log(tmp_path, data), Window(limit=10))
    notices = [turn for turn in page.turns if turn.speaker is Speaker.notice]
    assert len(notices) == 1
    assert notices[0].failed


def test_an_inline_image_in_a_result_is_named(tmp_path: Path) -> None:
    records = [
        {"type": "session", "id": "s", "cwd": "/home/user/p"},
        {
            "type": "message",
            "id": "r1",
            "message": {
                "role": "toolResult",
                "toolCallId": "c1",
                "content": [{"type": "image"}],
            },
        },
    ]
    data = ("\n".join(json.dumps(record) for record in records) + "\n").encode()
    page = history(_write_log(tmp_path, data), Window(limit=10))
    result = next(turn for turn in page.turns if turn.speaker is Speaker.tool)
    assert result.text == "[image]"


@pytest.mark.parametrize("hostile", ["../etc/passwd", "..", ".hidden", "a/b", "a\\b"])
def test_a_session_id_that_is_a_path_reads_nothing(tmp_path: Path, hostile: str) -> None:
    source = Source.of(hostile, agent_kind="omp", root=_omp_root(tmp_path))
    assert history(source).state is HistoryState.absent


def test_the_message_budget_reaches_past_a_huge_record(tmp_path: Path) -> None:
    big = "x" * (MAX_PAGE_BYTES + 10)
    records = [
        {"type": "session", "id": "s", "cwd": "/home/user/p"},
        {
            "type": "message",
            "id": "u1",
            "message": {"role": "user", "content": [{"type": "text", "text": "one"}]},
        },
        {
            "type": "message",
            "id": "r1",
            "message": {
                "role": "toolResult",
                "toolCallId": "c1",
                "content": [{"type": "text", "text": big}],
            },
        },
        {
            "type": "message",
            "id": "a1",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "two"}]},
        },
    ]
    data = ("\n".join(json.dumps(record) for record in records) + "\n").encode()
    page = history(_write_log(tmp_path, data), Window(messages=2))
    said = [turn.uuid for turn in page.turns if turn.speaker in (Speaker.human, Speaker.agent)]
    assert said == ["u1", "a1"]


# The tally


def test_the_tally_sums_tokens_money_and_prompts(tmp_path: Path) -> None:
    tally = usage(_source(tmp_path))
    assert tally.state is HistoryState.ok
    assert tally.messages == 3
    assert tally.prompts == 2
    assert (tally.input_tokens, tally.output_tokens) == (8853, 756)
    assert tally.cache_read_tokens == 16896
    assert tally.thinking_tokens == 388
    assert tally.cost == pytest.approx(0.00221283)
    # What went up with the newest answer, cache reads included.
    assert tally.context_tokens == 8668
    assert tally.models == [ModelRef("model-a", "vendor")]
    assert tally.latest == ModelRef("model-a", "vendor")
    assert [row.messages for row in tally.by_model] == [3]
    assert tally.turn_started == "2026-10-23T17:11:19.942Z"
    assert tally.last_at == "2026-10-23T17:11:37.099Z"


def test_the_session_says_where_it_started(tmp_path: Path) -> None:
    assert session_origin(_agent(), root=_omp_root(tmp_path)) == "/home/user/scratch/omp-ask"


# The Ask dialog, as the screen draws it


def _screen(name: str) -> str:
    return (OMP / f"{name}.txt").read_text()


def _lay(tmp_path: Path, session: str = PENDING) -> Path:
    """The fixture session laid out under a transcript root the app reads, and that root."""
    project = tmp_path / "transcripts" / "-home-user-scratch-omp-ask"
    project.mkdir(parents=True, exist_ok=True)
    (project / f"{session}.jsonl").write_bytes((TRANSCRIPTS / f"{session}.jsonl").read_bytes())
    return tmp_path / "transcripts"


def _ask(tmp_path: Path, session: str = PENDING) -> OmpAsk:
    """The pending ask the cut fixture's session record holds."""
    root = _lay(tmp_path, session)
    ask = pending_ask(Source.of(session, agent_kind="omp", root=root))
    assert ask is not None
    return ask


class _AskCase(NamedTuple):
    """One captured screen and the cut session record that holds its pending ask."""

    name: str
    session: str
    question: str
    labels: tuple[str, ...]


_ASK_CASES: Final = (
    _AskCase("ask-plain", PENDING, "Which fruit should I pick?", ("Apple", "Banana", "Cherry")),
    _AskCase(
        "ask-preview",
        PREVIEW_PENDING,
        "Which deploy plan should I use?",
        ("Canary", "Blue-green", "Big bang"),
    ),
)


def _ask_of(
    labels: list[str],
    descriptions: list[str] | None = None,
    *,
    question: str = "Which fruit should I pick?",
) -> OmpAsk:
    """An ask as omp writes one, for a screen a test composes."""
    return OmpAsk.model_validate(
        {
            "tool_call_id": "call-test",
            "questions": [
                {
                    "question": question,
                    "multi": False,
                    "options": [
                        {
                            "label": label,
                            "description": "" if descriptions is None else descriptions[i],
                            "preview": None,
                        }
                        for i, label in enumerate(labels)
                    ],
                }
            ],
        }
    )


def _labels(prompt: BlockedPrompt) -> list[str]:
    """The labels of the rows *prompt* draws."""
    return [option.label for option in prompt.options]


def _with_extra(text: str, anchor: str, after: list[str]) -> str:
    """*text* with *after* inserted following the first line holding *anchor*."""
    lines = text.splitlines()
    at = next(i for i, line in enumerate(lines) if anchor in line)
    lines[at + 1 : at + 1] = after
    return "\n".join(lines)


def _without(text: str, anchor: str) -> str:
    """*text* without every line holding *anchor*."""
    return "\n".join(line for line in text.splitlines() if anchor not in line)


def test_pending_ask_is_the_call_with_no_result_yet(tmp_path: Path) -> None:
    plain = _ask(tmp_path)
    assert plain.tool_call_id == "call_00000001"
    assert plain.menu is not None
    assert [option.label for option in plain.menu.options] == ["Apple", "Banana", "Cherry"]
    preview = _ask(tmp_path, PREVIEW_PENDING)
    assert preview.tool_call_id == "call_00000002"
    assert preview.menu is not None
    assert [option.label for option in preview.menu.options] == [
        "Canary",
        "Blue-green",
        "Big bang",
    ]
    # The captured session has both results, so neither ask is pending there.
    _lay(tmp_path, SESSION)
    answered = pending_ask(Source.of(SESSION, agent_kind="omp", root=tmp_path / "transcripts"))
    assert answered is None


@pytest.mark.parametrize("case", _ASK_CASES, ids=[case.name for case in _ASK_CASES])
def test_the_record_supplies_the_menu_the_screen_confirms(case: _AskCase, tmp_path: Path) -> None:
    ask = _ask(tmp_path, case.session)
    prompt = _parse(_screen(case.name), ask)
    assert prompt.question == case.question
    assert prompt.shape is MenuShape.SINGLE_CHOICE
    assert prompt.corroborated
    assert prompt.asked
    assert prompt.question_id == ask.tool_call_id
    assert [option.label for option in prompt.options] == list(case.labels)
    # Other is the screen's way into free text, never one of the record's rows.
    assert len(prompt.options) == len(case.labels)
    assert prompt.focus == 0
    assert prompt.digits_move
    assert [(hint.key, hint.verb) for hint in prompt.hints] == [("enter", "select")]
    assert [row.digit for row in decide_screen(prompt).options] == [""] * len(case.labels)


def _cursor_moved(text: str, target: str) -> str:
    """*text* with the dialog's cursor moved from its row onto *target*'s row."""
    lines = text.splitlines()
    cursor_row = max(i for i, line in enumerate(lines) if "\u276f" in line)
    target_row = max(i for i, line in enumerate(lines) if f"\u25cb {target}" in line)
    lines[cursor_row] = lines[cursor_row].replace("\u276f", " ")
    # Keep the mark in the row's own column: the cursor takes the space before it.
    lines[target_row] = "\u2502 \u276f \u25cb" + lines[target_row].split("\u25cb", 1)[1]
    return "\n".join(lines)


def test_moving_the_cursor_moves_what_enter_would_take(tmp_path: Path) -> None:
    ask = _ask(tmp_path)
    text = _screen("ask-plain")
    prompt = _parse(text, ask)
    other = _parse(_cursor_moved(text, "Banana"), ask)
    assert other.focus == 1
    assert other.identity != prompt.identity


# The Ask menu is inert: its rows are drawn, and the arrow keys answer it.

W = 117


def _box(rows: list[str], width: int = W) -> str:
    """An omp Ask dialog with these body rows, framed the way the fixture frames it."""
    top = "╭─ Ask " + "─" * (width - 7) + "╮"
    rule = "├" + "─" * width + "┤"
    bottom = "╰" + "─" * width + "╯"
    body = [f"│ {row:<{width - 1}}│" for row in rows]
    question = f"│ {'Which fruit should I pick?':<{width - 1}}│"
    foot = f"│ {'⏎ select · n note · ↑/↓ move · ⎋ cancel · Alt+H hide':<{width - 1}}│"
    return "\n".join(["", top, question, rule, *body, rule, foot, bottom])


def _blocked_herd(screen: str, *after: str, session: str = PENDING) -> FakeHerd:
    """One omp pane reporting *session* as a file; its reads are *screen*, then *after*."""
    herd = FakeHerd()
    herd.put(
        P3,
        agent="omp",
        status="blocked",
        cwd="/home/user/scratch/omp-ask",
        foreground_cwd="/home/user/scratch/omp-ask",
    )
    herd.agents[P3]["agent_session"] = {
        "agent": "omp",
        "kind": "path",
        "source": "herdr:omp",
        "value": _path(session),
    }
    herd.screens[P3] = [screen, *(after or ("> done",))]
    return herd


def _mismatched_screens() -> list[tuple[str, str]]:
    """The captured screen, changed one way at a time so the record can no longer confirm it."""
    text = _screen("ask-plain")
    return [
        ("label", text.replace("○ Banana", "○ Berry")),
        ("description", text.replace("Crisp, sweet-tart everyday classic.", "Crisp and sweet.")),
        ("extra", _with_extra(text, "       Crisp, sweet-tart", ["│       An extra line"])),
        ("missing", _without(text, "Crisp, sweet-tart")),
        ("fake-frame", _with_extra(text, "       Crisp, sweet-tart", ["│       ╭─ Ask ─╮"])),
        ("other-cursor", _cursor_moved(text, "Other (type your own)")),
        ("other-note", text.replace("○ Other (type your own)", "○ Other (type your own)  ✎ note")),
        ("cut-top", _without(text, "╭─ Ask")),
        ("cut-bottom", "\n".join(text.splitlines()[:-1])),
        ("cut-row", _without(text, "○ Cherry")),
    ]


@pytest.mark.parametrize(
    ("changed", "screen"),
    _mismatched_screens(),
    ids=[name for name, _ in _mismatched_screens()],
)
def test_a_screen_that_differs_from_the_record_draws_no_menu(
    changed: str, screen: str, tmp_path: Path
) -> None:
    """Any line the walk cannot account for forfeits the menu; the Pane still shows the screen."""
    ask = _ask(tmp_path)
    prompt = _parse(screen, ask)
    assert changed
    assert prompt.options == ()
    assert not prompt.question_id


def _records_differing(ask: OmpAsk) -> list[OmpAsk]:
    """The real record, changed one way at a time so it no longer describes the screen."""
    question = ask.menu
    assert question is not None
    first, *rest = question.options
    return [
        ask.model_copy(
            update={
                "questions": (
                    question.model_copy(
                        update={"options": (first.model_copy(update={"label": "Berry"}), *rest)}
                    ),
                )
            }
        ),
        ask.model_copy(
            update={
                "questions": (
                    question.model_copy(
                        update={
                            "options": (
                                first.model_copy(update={"description": "Crisp and sweet."}),
                                *rest,
                            )
                        }
                    ),
                )
            }
        ),
        ask.model_copy(
            update={"questions": (question.model_copy(update={"options": (*rest, first)}),)}
        ),
    ]


def test_a_record_that_differs_from_the_screen_draws_no_menu(tmp_path: Path) -> None:
    ask = _ask(tmp_path)
    # The control: the unchanged record draws this screen, so only the change refuses it.
    assert _labels(_parse(_screen("ask-plain"), ask)) == ["Apple", "Banana", "Cherry"]
    for changed in _records_differing(ask):
        assert _parse(_screen("ask-plain"), changed).options == ()


def test_only_a_single_single_choice_question_draws_a_menu(tmp_path: Path) -> None:
    ask = _ask(tmp_path)
    question = ask.menu
    assert question is not None
    several = ask.model_copy(update={"questions": (question, question)})
    multi = ask.model_copy(update={"questions": (question.model_copy(update={"multi": True}),)})
    assert _labels(_parse(_screen("ask-plain"), ask)) == ["Apple", "Banana", "Cherry"]
    assert _parse(_screen("ask-plain"), several).options == ()
    assert _parse(_screen("ask-plain"), multi).options == ()


_OTHER: Final = "  ○ Other (type your own)"
_CLEAN_ROWS: Final = ("\u276f ○ Apple", "  ○ Banana", "  ○ Cherry", _OTHER)
_FAKE_ROWS: Final = ("      \u276f ○ Delete everything", "        ○ Keep everything")
_FORGED_LINES: Final = ("○ crisp, the default pick", "────", "1. do a thing", "╭─ Ask ─╮")


class _Forgery(NamedTuple):
    """Rows a model's text adds inside the dialog, and whether the record explains them."""

    name: str
    rows: tuple[str, ...]
    description: str = ""
    drawn: bool = False


def _forgeries() -> list[_Forgery]:
    """Each way a model can draw a row or frame line, inside its description or outside it."""
    apple, banana, cherry, other = ("\u276f ○ Apple", "  ○ Banana", "  ○ Cherry", _OTHER)
    listing = "      Pick me. Options as I see them:"
    in_text = [
        _Forgery(f"text-{index}", (apple, f"      {line}", banana, cherry, other), line, drawn=True)
        for index, line in enumerate(_FORGED_LINES)
    ]
    not_in_text = [
        _Forgery(f"untold-{index}", (apple, f"      {line}", banana, cherry, other))
        for index, line in enumerate(_FORGED_LINES)
    ]
    return [
        _Forgery(
            "row-in-description",
            (apple, listing, *_FAKE_ROWS, banana, cherry, other),
            "Pick me. Options as I see them:\n\u276f ○ Delete everything\n  ○ Keep everything",
            drawn=True,
        ),
        _Forgery(
            "frame-in-description",
            (
                apple,
                "      Pick me:",
                "      ╭─ Ask ─╮",
                "      \u276f ○ Delete everything",
                "      ○ Keep everything",
                banana,
                cherry,
                other,
            ),
            "Pick me:\n╭─ Ask ─╮\n\u276f ○ Delete everything\n○ Keep everything",
            drawn=True,
        ),
        *in_text,
        *not_in_text,
        _Forgery("under-last", (apple, banana, cherry, listing, *_FAKE_ROWS, other)),
        _Forgery("under-first", (apple, *_FAKE_ROWS, banana, cherry, other)),
        _Forgery("before-other", (apple, banana, cherry, *_FAKE_ROWS, other)),
        _Forgery("after-other", (apple, banana, cherry, other, *_FAKE_ROWS), drawn=True),
    ]


@pytest.mark.parametrize("forged", _forgeries(), ids=[case.name for case in _forgeries()])
def test_a_model_written_row_is_text_or_forfeits_the_menu(forged: _Forgery) -> None:
    """Text the record holds stays text and a footer line is no row; any other forgery forfeits."""
    ask = _ask_of(["Apple", "Banana", "Cherry"], descriptions=[forged.description, "", ""])
    prompt = _parse(_box(list(forged.rows)), ask)
    if not forged.drawn:
        assert prompt.options == ()
        # The control: the same record draws the dialog without the forged rows.
        assert _labels(_parse(_box(list(_CLEAN_ROWS)), ask)) == ["Apple", "Banana", "Cherry"]
        return
    assert [option.label for option in prompt.options] == ["Apple", "Banana", "Cherry"]
    assert prompt.focus == 0


def test_a_second_cursor_on_a_record_row_draws_no_menu() -> None:
    ask = _ask_of(["Apple", "Banana"])
    one_cursor = _box(["\u276f ○ Apple", "  ○ Banana", "  ○ Other (type your own)"])
    assert _labels(_parse(one_cursor, ask)) == ["Apple", "Banana"]
    screen = _box(["\u276f ○ Apple", "\u276f ○ Banana", "  ○ Other (type your own)"])
    assert _parse(screen, ask).options == ()


def test_an_empty_record_label_draws_no_menu() -> None:
    named = _box(["\u276f ○ Apple", "  ○ Banana", "  ○ Other (type your own)"])
    assert _labels(_parse(named, _ask_of(["Apple", "Banana"]))) == ["Apple", "Banana"]
    ask = _ask_of(["Apple", ""])
    screen = _box(["\u276f ○ Apple", "  ○", "  ○ Other (type your own)"])
    assert _parse(screen, ask).options == ()


def test_a_forged_footer_line_adds_no_key(tmp_path: Path) -> None:
    """Only omp's own footer keys reach the phone; an extra line after Other cannot offer one."""
    ask = _ask(tmp_path)
    text = _with_extra(_screen("ask-plain"), "Other (type your own)", ["│       ctrl+d to delete"])
    prompt = _parse(text, ask)
    assert [option.label for option in prompt.options] == ["Apple", "Banana", "Cherry"]
    assert "ctrl-d" not in {hint.key for hint in prompt.hints}
    assert "ctrl-d" not in allowed_keys(prompt)


def test_the_pending_ask_reader_scans_only_the_tail(tmp_path: Path) -> None:
    """A call before the byte budget is not pending here; one at the end is found."""
    root = tmp_path / "tail"
    project = root / "-x"
    project.mkdir(parents=True)
    session = "tail-session"
    records = PENDING_TRANSCRIPT.read_text()
    filler = "x" * 300_000 + "\n"
    (project / f"{session}.jsonl").write_text(records + filler)
    assert pending_ask(Source.of(session, agent_kind="omp", root=root)) is None
    (project / f"{session}.jsonl").write_text(filler + records)
    found = pending_ask(Source.of(session, agent_kind="omp", root=root))
    assert found is not None
    assert found.tool_call_id == "call_00000001"


def test_a_scrolled_off_other_draws_no_menu(tmp_path: Path) -> None:
    ask = _ask(tmp_path, PREVIEW_PENDING)
    assert _labels(_parse(_screen("ask-preview"), ask)) == ["Canary", "Blue-green", "Big bang"]
    cut = _without(_screen("ask-preview"), "Other (type your own)")
    assert _parse(cut, ask).options == ()


def test_the_record_menu_is_omps_alone(tmp_path: Path) -> None:
    """Only an omp pane's source carries a record question, so every other kind keeps main's parse."""
    root = _lay(tmp_path)
    for kind in ("", "claude", "opencode", "hermes"):
        assert pending_ask(Source.of(PENDING, agent_kind=kind, root=root)) is None
        assert pending_question(Source.of(PENDING, agent_kind=kind, root=root)) is None
        other = _agent(PENDING).model_copy(update={"agent": kind})
        assert prompt_source(other, root).question is None
    assert pending_ask(Source.of(PENDING, agent_kind="omp", root=root)) is not None
    found = pending_question(Source.of(PENDING, agent_kind="omp", root=root))
    assert found is not None
    assert found.id == "call_00000001"
    assert prompt_source(_agent(PENDING), root).question == found


def test_a_numbered_screen_keeps_main_parse_and_draws_no_record_menu(tmp_path: Path) -> None:
    ask = _ask(tmp_path)
    path = FIXTURES / "capture-blocked-askuserquestion-menu" / "visible.txt"
    base = path.read_text()
    numbered = BlockedPrompt.parse(base, source=PromptSource(agent_kind="claude"))
    assert [option.number for option in numbered.options] == [1, 2, 3, 4, 5]
    # The record cannot confirm a numbered screen, so an omp pane draws no rows at all.
    assert _parse(base, ask).options == ()


def test_enter_is_refused_when_the_cursor_moved_since_it_was_drawn(tmp_path: Path) -> None:
    """The question's identity carries the record's call and cursor row, so a stale key is refused."""
    ask = _ask(tmp_path)
    drawn = _parse(_screen("ask-plain"), ask)
    moved_screen = _cursor_moved(_screen("ask-plain"), "Banana")
    moved = _parse(moved_screen, ask)
    assert moved.focus == 1
    assert moved.identity != drawn.identity
    herd = _blocked_herd(moved_screen)
    writer = replace(rig(tmp_path, herd).writer, stores=(str(tmp_path / "transcripts"),))
    key = KeyAsk(
        as_session_id(PENDING),
        "enter",
        expected_target=as_pane_id(P3),
        drawn_from=control_token(drawn),
    )
    got = send_key(writer, key)
    assert isinstance(got, Refused)
    assert got.refusal is Refusal.SCREEN_CHANGED
    assert not herd.writes("agent.send_keys")


def test_a_new_ask_id_refuses_the_old_key(tmp_path: Path) -> None:
    """The same screen and labels still change identity when the record's call changes."""
    ask = _ask(tmp_path)
    drawn = _parse(_screen("ask-plain"), ask)
    path = tmp_path / "transcripts" / "-home-user-scratch-omp-ask" / f"{PENDING}.jsonl"
    path.write_text(PENDING_TRANSCRIPT.read_text().replace("call_00000001", "call_99999999"))
    herd = _blocked_herd(_screen("ask-plain"))
    writer = replace(rig(tmp_path, herd).writer, stores=(str(tmp_path / "transcripts"),))
    key = KeyAsk(
        as_session_id(PENDING),
        "enter",
        expected_target=as_pane_id(P3),
        drawn_from=control_token(drawn),
    )
    got = send_key(writer, key)
    assert isinstance(got, Refused)
    assert got.refusal is Refusal.SCREEN_CHANGED
    assert not herd.writes("agent.send_keys")


def test_the_down_key_reaches_omp_and_the_identity_follows_the_cursor(tmp_path: Path) -> None:
    """The phone's down key passes the fresh re-check, is sent, and the watch reads the moved row."""
    ask = _ask(tmp_path)
    drawn = _parse(_screen("ask-plain"), ask)
    moved = _cursor_moved(_screen("ask-plain"), "Banana")
    herd = _blocked_herd(_screen("ask-plain"), moved)
    writer = replace(rig(tmp_path, herd).writer, stores=(str(tmp_path / "transcripts"),))
    key = KeyAsk(
        as_session_id(PENDING),
        "down",
        expected_target=as_pane_id(P3),
        drawn_from=control_token(drawn),
    )
    got = send_key(writer, key)
    assert isinstance(got, KeySent)
    assert [params.get("keys") for params in herd.writes("agent.send_keys")] == [["down"]]
    assert got.outcome is Outcome.CONFIRMED
    assert got.prompt is not None
    assert got.prompt.question_id == ask.tool_call_id
    assert got.prompt.focus == 1
    assert got.prompt.identity != drawn.identity


class _Pane(NamedTuple):
    """What one agent's pane shows: its status and the screen it draws."""

    status: str = "idle"
    screen: str = "> "


_IDLE: Final = _Pane()


def _rig(tmp_path: Path, pane: _Pane = _IDLE, *, session: str = PENDING) -> Rig:
    """An app whose only agent is omp, its pending ask under the app's transcript root."""
    _lay(tmp_path, session)
    frame = snapshot_frame()
    raw = next(agent for agent in agents_of(frame) if agent.get("pane_id") == WORKING_PANE)
    raw["agent"] = "omp"
    raw["agent_status"] = pane.status
    raw["agent_session"] = {
        "agent": "omp",
        "kind": "path",
        "source": "herdr:omp",
        "value": _path(session),
    }
    return build_rig(tmp_path, {SNAPSHOT: frame, READ: read_frame(pane.screen)})


def _focused_label(html: str) -> str:
    """The label of the row the answer region marks as the cursor's, in one answer fragment."""
    tail = html.split("option--on", 1)[1]
    return tail.split('class="option__label">', 1)[1].split("<", 1)[0]


def test_the_answer_menu_follows_a_cursor_move_on_the_next_read(tmp_path: Path) -> None:
    """Polls share one prompt read until the cache lets it go; the next read shows the moved cursor."""
    rig = _rig(tmp_path, _Pane("blocked", _screen("ask-plain")))
    assert _focused_label(rig.http.get(f"/agent/{PENDING}/answer").text) == "Apple"
    rig.wire.replies[READ] = read_frame(_cursor_moved(_screen("ask-plain"), "Banana"))
    assert _focused_label(rig.http.get(f"/agent/{PENDING}/answer").text) == "Apple"
    rig.clock.now += rig.settings.snapshot_cache_ttl
    assert _focused_label(rig.http.get(f"/agent/{PENDING}/answer").text) == "Banana"


def _append_result(root: Path) -> None:
    """Append the result that answers the pending fixture's ask call to its session log."""
    path = root / "-home-user-scratch-omp-ask" / f"{PENDING}.jsonl"
    result = {
        "type": "message",
        "id": "00000099",
        "message": {
            "role": "toolResult",
            "toolCallId": "call_00000001",
            "content": [{"type": "text", "text": "Apple"}],
        },
    }
    with path.open("a") as handle:
        handle.write(json.dumps(result) + "\n")


def test_the_prompt_stamp_and_cache_follow_the_session_file(tmp_path: Path) -> None:
    """The prompt cache's stamp moves with the log, and the ask scan is invalidated with it."""
    _ask(tmp_path)
    agent = _agent(PENDING)
    root = tmp_path / "transcripts"
    before = prompt_stamp(agent, root)
    _append_result(root)
    assert prompt_stamp(agent, root) != before
    assert pending_ask(Source.of(PENDING, agent_kind="omp", root=root)) is None


def test_the_composer_is_held_by_the_record_menu(tmp_path: Path) -> None:
    """The composer and the answer region agree on the record, unlike a screen-only parse."""
    rig = _rig(tmp_path, _Pane("blocked", _screen("ask-plain")))
    page = rig.http.get(f"/agent/{PENDING}/compose").text
    assert "composer--held" in page
    assert "Answer the question above first" in page
    # A plain parse holds the composer too; only the record menu puts a cursor on a record row.
    assert _focused_label(rig.http.get(f"/agent/{PENDING}/answer").text) == "Apple"


def test_the_answer_page_turns_the_record_into_inert_controls(tmp_path: Path) -> None:
    rig = _rig(tmp_path, _Pane("blocked", _screen("ask-plain")))
    page = rig.http.get(f"/agent/{PENDING}/answer").text
    assert "Which fruit should I pick?" in page
    for label in ("Apple", "Banana", "Cherry"):
        assert label in page
    # Other is the pane's way into free text; the record has three choices and draws three rows.
    assert "Other (type your own)" not in page
    # The rows are inert: no row is a form, the cursor's row is marked, and the pad keys answer.
    assert 'value="row' not in page
    assert "option--still" in page
    assert "Rows aren't buttons here" in page
    assert "option--on" in page
    for key in ("up", "down", "enter", "esc"):
        assert f'name="key" value="{key}"' in page
    # A digit is no answer on this screen, so none is offered.
    for digit in ("1", "2", "3"):
        assert f'name="key" value="{digit}"' not in page


def test_the_agents_own_page_draws_the_ask_menu_while_the_record_holds_it(tmp_path: Path) -> None:
    """The page's answer region opens on the record's menu, and loses it once the record answers."""
    rig = _rig(tmp_path, _Pane("blocked", _screen("ask-plain")))
    page = rig.http.get(f"/agent/{PENDING}").text
    answer = page.split('<section id="answer"', 1)[1].split("</section>", 1)[0]
    assert _focused_label(answer) == "Apple"
    assert "Other (type your own)" not in answer
    _append_result(tmp_path / "transcripts")
    page = rig.http.get(f"/agent/{PENDING}").text
    answer = page.split('<section id="answer"', 1)[1].split("</section>", 1)[0]
    assert "option--on" not in answer


def test_the_chat_tab_shows_omps_conversation(tmp_path: Path) -> None:
    rig = _rig(tmp_path, session=SESSION)
    chat = rig.http.get(f"/agent/{SESSION}/chat", headers={"HX-Request": "true"}).text
    assert "Use your ask tool right away" in chat
    assert "Asking which fruit to pick" in chat
    assert "User selected: Apple" in chat
