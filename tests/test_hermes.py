"""The Hermes adapter, against Hermes's own schema.

``tests/fixtures/transcript/hermes/schema.sql`` is verbatim `.schema` for the
three tables this adapter reads, out of a real ``~/.hermes/state.db``. The ROWS
below are written here rather than captured, and that is a deliberate departure
from the rule the opencode fixture states -- *a fixture in this repo is a
capture or it is not a fixture*.

The reason is that Hermes keeps ONE database for every session on the machine,
so a capture is not a copy of one conversation the way Claude's per-session
JSONL is: it is the operator's whole history, their prompts and their paths, and
the scrub cannot make prose anonymous the way it can make a username
``owner``. So the half that must be real is real -- the DDL, replayed verbatim,
which is what catches a column being renamed underneath us -- and the rows are
built to cover every shape the adapter branches on:

* a person's prompt, and an assistant's prose
* an assistant row carrying ONE tool call
* an assistant row carrying FOUR, which is the shape Hermes actually writes and
  Claude almost never does (see ``_call_turn``)
* a ``tool`` row pairing back by ``tool_call_id``
* a row left behind by a compaction, flagged ``active = 0``
* an empty assistant row that is neither prose nor a call
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from herdr_pwa.engine.hermes import HERMES_DB, HermesAdapter
from herdr_pwa.engine.transcript import (
    AGENT_KINDS,
    HistoryState,
    Speaker,
    adapter_for,
    fold_tool_results,
)
from herdr_pwa.usage import UsageField, source_for

STORE = Path(__file__).parent / "fixtures" / "transcript" / "hermes"
SCHEMA = (STORE / "schema.sql").read_text()
SESSION = "20260101_120000_aaaaaa"
OTHER = "20260101_130000_bbbbbb"


def call(identifier: str, tool: str, **arguments: object) -> dict[str, object]:
    """One entry of the ``tool_calls`` JSON array, shaped the way Hermes writes it."""
    return {
        "id": identifier,
        "call_id": identifier,
        "type": "function",
        "function": {"name": tool, "arguments": json.dumps(arguments)},
    }


ROWS: tuple[dict[str, object], ...] = (
    {"role": "user", "content": "list the files", "timestamp": 1780000001.0},
    {
        "role": "assistant",
        "content": "",
        "tool_calls": json.dumps([call("call_a", "terminal", command="ls -la")]),
        "timestamp": 1780000002.0,
    },
    {
        "role": "tool",
        "content": '{"output": "total 0"}',
        "tool_call_id": "call_a",
        "timestamp": 1780000003.0,
    },
    {"role": "assistant", "content": "Two files.", "timestamp": 1780000004.0},
    {
        "role": "assistant",
        "content": "",
        "tool_calls": json.dumps(
            [
                call("call_b", "terminal", command="pwd"),
                call("call_c", "skill_view", name="herd"),
                call("call_d", "skill_view", name="wave"),
                call("call_e", "write_file", path="/srv/x", content="hi"),
            ]
        ),
        "timestamp": 1780000005.0,
    },
    {"role": "assistant", "content": "", "timestamp": 1780000006.0},
    {
        "role": "user",
        "content": "compacted away",
        "timestamp": 1780000007.0,
        "active": 0,
    },
)


def build(path: Path, rows: tuple[dict[str, object], ...] = ROWS) -> Path:
    """A database with Hermes's own schema, holding exactly *rows*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        for name in (SESSION, OTHER):
            db.execute(
                "insert into sessions (id, source, started_at)"
                " values (?, 'cli', 1780000000.0)",
                (name,),
            )
        for row in rows:
            record = {"session_id": SESSION, **row}
            columns = ", ".join(f'"{key}"' for key in record)
            marks = ", ".join("?" for _ in record)
            db.execute(
                f"insert into messages ({columns}) values ({marks})",  # noqa: S608
                tuple(record.values()),
            )
    return path


@pytest.fixture
def roots(tmp_path: Path) -> list[Path]:
    """A root holding one store, built for this test."""
    build(tmp_path / HERMES_DB)
    return [tmp_path]


def turns(roots: list[Path], **kwargs: object):
    """The turns of one page, for the assertions that only care about those."""
    return HermesAdapter().page(SESSION, roots=roots, **kwargs).turns  # type: ignore[arg-type]


# --- the registry ----------------------------------------------------------


def test_hermes_is_a_kind_the_chat_tab_can_answer_for() -> None:
    """Being absent from either of these is what left the Chat tab silent."""
    assert "hermes" in AGENT_KINDS
    assert adapter_for("hermes") is not None


# --- what a row becomes ----------------------------------------------------


def test_a_persons_prompt_and_the_answer_to_it_are_both_conversation(
    roots: list[Path],
) -> None:
    found = turns(roots)
    assert found[0].speaker is Speaker.human
    assert found[0].text == "list the files"
    assert any(t.speaker is Speaker.agent and t.text == "Two files." for t in found)


def test_a_tool_call_carries_its_name_and_its_identifying_argument(
    roots: list[Path],
) -> None:
    """The summary is the one argument that says WHICH call this was."""
    first = next(t for t in turns(roots) if t.tool == "terminal")
    assert first.speaker is Speaker.tool
    assert first.text == "ls -la"
    assert first.call == "call_a"


def test_a_result_is_folded_onto_the_call_it_answers(roots: list[Path]) -> None:
    """``tool_call_id`` is the only thing pairing them; they are separate rows."""
    folded = fold_tool_results(turns(roots))
    call_turn = next(t for t in folded if t.call == "call_a")
    assert call_turn.result == '{"output": "total 0"}'
    assert not any(t.tool == "result" and t.call == "call_a" for t in folded)


def test_an_assistant_row_with_four_calls_collapses_to_the_first(
    roots: list[Path],
) -> None:
    """Claude's rule, applied to a harness that batches far harder than Claude does.

    One ROW is one position in the stream, and the cursor is a position, so a
    row may not become four turns without the frontier meaning something
    different at the top of a page than at the bottom. The other three calls are
    reachable in the Pane tab, which is verbatim.
    """
    batched = [t for t in turns(roots) if t.call in {"call_b", "call_c", "call_d"}]
    assert [t.call for t in batched] == ["call_b"]
    assert batched[0].tool == "terminal"


def test_a_row_that_is_neither_prose_nor_a_call_is_dropped(roots: list[Path]) -> None:
    """An empty assistant row would otherwise be an empty bubble."""
    assert all(t.text or t.tool for t in turns(roots))


def test_a_compacted_row_is_not_conversation_any_more(roots: list[Path]) -> None:
    """``active = 0`` is what a compaction leaves behind, and it is not history."""
    assert not any(t.text == "compacted away" for t in turns(roots))


# --- paging ----------------------------------------------------------------


def test_a_page_ends_where_it_says_it_does_and_the_next_one_resumes_there(
    roots: list[Path],
) -> None:
    adapter = HermesAdapter()
    tail = adapter.page(SESSION, roots=roots, limit=2)
    assert tail.has_more
    head = adapter.page(SESSION, roots=roots, limit=2, before=tail.cursor)
    assert head.end == tail.cursor
    assert {t.uuid for t in head.turns}.isdisjoint({t.uuid for t in tail.turns})


def test_since_reads_only_what_arrived_after_the_frontier(roots: list[Path]) -> None:
    """This is what makes the chat APPEND rather than re-render."""
    adapter = HermesAdapter()
    whole = adapter.page(SESSION, roots=roots)
    assert adapter.since(SESSION, roots=roots, after=whole.end).turns == []
    grown = adapter.since(SESSION, roots=roots, after=0)
    assert [t.uuid for t in grown.turns] == [t.uuid for t in whole.turns]


def test_a_frontier_past_the_end_reports_a_reset_rather_than_guessing(
    roots: list[Path],
) -> None:
    """A cursor bigger than the store means the held cursor means nothing."""
    page = HermesAdapter().since(SESSION, roots=roots, after=10_000)
    assert page.reset
    assert page.turns == []


# --- the store itself ------------------------------------------------------


def test_a_session_in_no_store_is_absent_rather_than_empty(roots: list[Path]) -> None:
    """``absent`` and ``empty`` are different sentences on the screen."""
    page = HermesAdapter().page("20260101_000000_zzzzzz", roots=roots)
    assert page.state is HistoryState.absent


def test_a_session_with_no_messages_is_empty_rather_than_absent(
    roots: list[Path],
) -> None:
    page = HermesAdapter().page(OTHER, roots=roots)
    assert page.state is HistoryState.empty


def test_one_sessions_page_never_reaches_another_sessions_rows(
    tmp_path: Path,
) -> None:
    """One file holds every session on the machine, so this is the whole safety."""
    path = build(tmp_path / HERMES_DB)
    with sqlite3.connect(path) as db:
        db.execute(
            "insert into messages (session_id, role, content, timestamp)"
            " values (?, 'user', 'not yours', 1780000009.0)",
            (OTHER,),
        )
    assert not any(t.text == "not yours" for t in turns([tmp_path]))


def test_a_directory_with_no_store_is_absent(tmp_path: Path) -> None:
    assert HermesAdapter().page(SESSION, roots=[tmp_path]).state is HistoryState.absent


def test_a_file_that_is_not_hermes_is_refused_rather_than_read(
    tmp_path: Path,
) -> None:
    """A database with the right NAME and the wrong schema is not a store."""
    with sqlite3.connect(tmp_path / HERMES_DB) as db:
        db.execute("create table something_else (id text)")
    assert HermesAdapter().page(SESSION, roots=[tmp_path]).state is HistoryState.absent


def test_the_store_is_opened_read_only(roots: list[Path]) -> None:
    """A live Hermes is writing to this file; this app may never be why it fails.

    Not a claim about intent -- the URI carries ``mode=ro``, so a write raises
    rather than relying on the reader never attempting one.
    """
    from herdr_pwa.engine.hermes import _readonly  # noqa: PLC2701

    uri = _readonly(roots[0] / HERMES_DB)
    assert "mode=ro" in uri
    with sqlite3.connect(uri, uri=True) as db, pytest.raises(sqlite3.OperationalError):
        db.execute(
            "insert into sessions (id, source, started_at)"
            " values ('x', 'cli', 1780000000.0)"
        )


# --- usage -----------------------------------------------------------------


def test_usage_is_summed_off_the_rollup_hermes_already_keeps(
    tmp_path: Path,
) -> None:
    """Hermes tallies per MODEL, not per message, so nothing is re-derived."""
    path = build(tmp_path / HERMES_DB)
    with sqlite3.connect(path) as db:
        db.execute(
            "insert into session_model_usage (session_id, model, billing_provider,"
            " api_call_count, input_tokens, output_tokens, cache_read_tokens)"
            " values (?, 'a-model', 'custom', 3, 100, 20, 40)",
            (SESSION,),
        )
    spent = HermesAdapter().tally(SESSION, roots=[tmp_path])
    assert spent.messages == 3
    assert spent.input_tokens == 100
    assert spent.output_tokens == 20
    assert spent.cache_read_tokens == 40
    assert [ref.model for ref in spent.models] == ["a-model"]


def test_cost_stays_zero_because_hermes_records_no_money(tmp_path: Path) -> None:
    """Zero means UNKNOWN here, and a price table would be a worse answer."""
    build(tmp_path / HERMES_DB)
    assert HermesAdapter().tally(SESSION, roots=[tmp_path]).cost == 0.0


# --- the session's own record ----------------------------------------------


def named(path: Path, title: str, model: str = "a-model") -> None:
    """Give SESSION a title and a model, the way hermes does after a turn."""
    with sqlite3.connect(path) as db:
        db.execute(
            "update sessions set title = ?, model = ? where id = ?",
            (title, model, SESSION),
        )


def test_the_title_hermes_generated_beats_a_shell_prompt(tmp_path: Path) -> None:
    """A hermes pane's terminal title is `user@host:cwd`, which says nothing.

    `card_title` already falls back to `session_title` when the terminal title
    is not earned, so answering this is the whole of what a card needs.
    """
    path = build(tmp_path / HERMES_DB)
    named(path, "Plan the migration")
    assert HermesAdapter().info(SESSION, roots=[tmp_path])[0] == "Plan the migration"


def test_a_session_with_no_store_has_no_title_rather_than_raising(
    tmp_path: Path,
) -> None:
    assert HermesAdapter().info(SESSION, roots=[tmp_path]) == ("", "")


# --- usage -----------------------------------------------------------------


def spend(path: Path, **columns: object) -> None:
    """One `session_model_usage` row, defaulted to the shape hermes writes."""
    row = {
        "session_id": SESSION,
        "model": "a-model",
        "billing_provider": "custom",
        "api_call_count": 3,
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_tokens": 40,
        "cache_write_tokens": 5,
        "reasoning_tokens": 7,
        "cost_status": "unknown",
        **columns,
    }
    with sqlite3.connect(path) as db:
        names = ", ".join(f'"{k}"' for k in row)
        marks = ", ".join("?" for _ in row)
        db.execute(
            f"insert into session_model_usage ({names}) values ({marks})",  # noqa: S608
            tuple(row.values()),
        )


def test_reasoning_and_cache_writes_are_read_rather_than_left_at_zero(
    tmp_path: Path,
) -> None:
    """Both are columns hermes fills; a real session had 88625 reasoning tokens."""
    path = build(tmp_path / HERMES_DB)
    spend(path)
    spent = HermesAdapter().tally(SESSION, roots=[tmp_path])
    assert spent.thinking_tokens == 7
    assert spent.cache_creation_tokens == 5


def test_money_is_reported_only_where_hermes_says_it_knows(tmp_path: Path) -> None:
    """`cost_status` is the authority, not the column: it writes 0.0 either way.

    Reading the number without the status turns "this provider does not price
    itself" into "this session was free", and zero already means UNKNOWN in
    `SessionUsage.cost`.
    """
    path = build(tmp_path / HERMES_DB)
    spend(path, cost_status="unknown", estimated_cost_usd=1.5)
    assert HermesAdapter().tally(SESSION, roots=[tmp_path]).cost == 0.0


def test_a_priced_row_is_reported(tmp_path: Path) -> None:
    path = build(tmp_path / HERMES_DB)
    spend(path, cost_status="actual", actual_cost_usd=2.5, estimated_cost_usd=1.5)
    assert HermesAdapter().tally(SESSION, roots=[tmp_path]).cost == 2.5


def test_hermes_is_a_usage_source_at_all() -> None:
    """Being absent from `_SOURCES` is why the chat tab showed no numbers."""
    source = source_for("hermes")
    assert source is not None
    assert UsageField.model in source.fields
    assert UsageField.output_tokens in source.fields


def test_context_is_not_claimed_because_hermes_does_not_record_it() -> None:
    """hermes DRAWS `~43.7K/1M` and stores nothing behind it.

    `messages.token_count` is NULL on every row of a real session and the
    per-model rollup is cumulative across every call, so the only honest answer
    is no answer. A cumulative total in this field would read as a context
    window several times its own size.
    """
    source = source_for("hermes")
    assert source is not None
    assert UsageField.context_tokens not in source.fields
    assert UsageField.context_pct not in source.fields
