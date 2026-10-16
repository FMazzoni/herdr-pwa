"""The Hermes adapter, against Hermes's own schema replayed verbatim with rows written here."""

import json
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest

from herdr_pwa.engine import HistoryState, Source, Speaker, TranscriptTurn
from tests.engine_seam import (
    AGENT_KINDS,
    HERMES_DB,
    HermesAdapter,
    Window,
    adapter_for,
    fold_tool_results,
    readonly_uri,
)

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
                "insert into sessions (id, source, started_at) values (?, 'cli', 1780000000.0)",
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


def turns(roots: list[Path]) -> list[TranscriptTurn]:
    """The turns of one page, for the assertions that only care about those."""
    return HermesAdapter().page(Source(SESSION, roots), Window()).turns


def test_hermes_is_a_kind_the_chat_tab_can_answer_for() -> None:
    """Being absent from either of these is what left the Chat tab silent."""
    assert "hermes" in AGENT_KINDS
    assert adapter_for("hermes") is not None


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
    """One row is one stream position, so it may not become four turns under one cursor."""
    batched = [t for t in turns(roots) if t.call in {"call_b", "call_c", "call_d"}]
    assert [t.call for t in batched] == ["call_b"]
    assert batched[0].tool == "terminal"


def test_a_row_that_is_neither_prose_nor_a_call_is_dropped(roots: list[Path]) -> None:
    """An empty assistant row would otherwise be an empty bubble."""
    assert all(t.text or t.tool for t in turns(roots))


def test_a_compacted_row_is_not_conversation_any_more(roots: list[Path]) -> None:
    """``active = 0`` is what a compaction leaves behind, and it is not history."""
    assert not any(t.text == "compacted away" for t in turns(roots))


def test_a_page_ends_where_it_says_it_does_and_the_next_one_resumes_there(
    roots: list[Path],
) -> None:
    adapter = HermesAdapter()
    tail = adapter.page(Source(SESSION, roots), Window(limit=2))
    assert tail.has_more
    head = adapter.page(Source(SESSION, roots), Window(limit=2, before=tail.cursor))
    assert head.end == tail.cursor
    assert {t.uuid for t in head.turns}.isdisjoint({t.uuid for t in tail.turns})


def test_since_reads_only_what_arrived_after_the_frontier(roots: list[Path]) -> None:
    """This is what makes the chat APPEND rather than re-render."""
    adapter = HermesAdapter()
    whole = adapter.page(Source(SESSION, roots), Window())
    assert adapter.since(Source(SESSION, roots), Window(after=whole.end)).turns == []
    grown = adapter.since(Source(SESSION, roots), Window(after=0))
    assert [t.uuid for t in grown.turns] == [t.uuid for t in whole.turns]


def test_a_frontier_past_the_end_reports_a_reset_rather_than_guessing(
    roots: list[Path],
) -> None:
    """A cursor bigger than the store means the held cursor means nothing."""
    page = HermesAdapter().since(Source(SESSION, roots), Window(after=10_000))
    assert page.reset
    assert page.turns == []


def test_a_session_in_no_store_is_absent_rather_than_empty(roots: list[Path]) -> None:
    """``absent`` and ``empty`` are different sentences on the screen."""
    page = HermesAdapter().page(Source("20260101_000000_zzzzzz", roots), Window())
    assert page.state is HistoryState.absent


def test_a_session_with_no_messages_is_empty_rather_than_absent(
    roots: list[Path],
) -> None:
    page = HermesAdapter().page(Source(OTHER, roots), Window())
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
    assert HermesAdapter().page(Source(SESSION, [tmp_path]), Window()).state is HistoryState.absent


def test_a_file_that_is_not_hermes_is_refused_rather_than_read(
    tmp_path: Path,
) -> None:
    """A database with the right NAME and the wrong schema is not a store."""
    with sqlite3.connect(tmp_path / HERMES_DB) as db:
        db.execute("create table something_else (id text)")
    assert HermesAdapter().page(Source(SESSION, [tmp_path]), Window()).state is HistoryState.absent


def test_the_store_is_opened_read_only(roots: list[Path]) -> None:
    """A live Hermes is writing to this file; the URI's ``mode=ro`` makes a write raise."""
    uri = readonly_uri(roots[0] / HERMES_DB)
    assert "mode=ro" in uri
    with sqlite3.connect(uri, uri=True) as db, pytest.raises(sqlite3.OperationalError):
        db.execute(
            "insert into sessions (id, source, started_at) values ('x', 'cli', 1780000000.0)"
        )


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
    spent = HermesAdapter().tally(Source(SESSION, [tmp_path]))
    assert spent.messages == 3
    assert spent.input_tokens == 100
    assert spent.output_tokens == 20
    assert spent.cache_read_tokens == 40
    assert [ref.model for ref in spent.models] == ["a-model"]


def test_cost_stays_zero_because_hermes_records_no_money(tmp_path: Path) -> None:
    """Zero means UNKNOWN here, and a price table would be a worse answer."""
    build(tmp_path / HERMES_DB)
    assert HermesAdapter().tally(Source(SESSION, [tmp_path])).cost == 0.0


def named(path: Path, title: str, model: str = "a-model") -> None:
    """Give SESSION a title and a model, the way hermes does after a turn."""
    with sqlite3.connect(path) as db:
        db.execute(
            "update sessions set title = ?, model = ? where id = ?",
            (title, model, SESSION),
        )


def test_the_title_hermes_generated_beats_a_shell_prompt(tmp_path: Path) -> None:
    """A hermes pane's terminal title is `user@host:cwd`, which says nothing."""
    path = build(tmp_path / HERMES_DB)
    named(path, "Plan the migration")
    assert HermesAdapter().info(Source(SESSION, [tmp_path]))[0] == "Plan the migration"


def test_a_session_with_no_store_has_no_title_rather_than_raising(
    tmp_path: Path,
) -> None:
    assert HermesAdapter().info(Source(SESSION, [tmp_path])) == ("", "")


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
    spent = HermesAdapter().tally(Source(SESSION, [tmp_path]))
    assert spent.thinking_tokens == 7
    assert spent.cache_creation_tokens == 5


def test_money_is_reported_only_where_hermes_says_it_knows(tmp_path: Path) -> None:
    """`cost_status` is the authority, not the column: it writes 0.0 either way."""
    path = build(tmp_path / HERMES_DB)
    spend(path, cost_status="unknown", estimated_cost_usd=1.5)
    assert HermesAdapter().tally(Source(SESSION, [tmp_path])).cost == 0.0


def test_a_priced_row_is_reported(tmp_path: Path) -> None:
    path = build(tmp_path / HERMES_DB)
    spend(path, cost_status="actual", actual_cost_usd=2.5, estimated_cost_usd=1.5)
    assert HermesAdapter().tally(Source(SESSION, [tmp_path])).cost == 2.5


@contextmanager
def held(path: Path) -> Generator[None]:
    """Hermes mid-write: an exclusive transaction blocks every other connection, readers too."""
    holder = sqlite3.connect(path)
    holder.execute("begin exclusive")
    try:
        yield
    finally:
        holder.rollback()
        holder.close()


def test_a_locked_store_reads_as_unreadable_not_empty(roots: list[Path]) -> None:
    """A writer holding the store must not read as "nothing said yet"."""
    adapter = HermesAdapter()
    with held(roots[0] / HERMES_DB):
        page = adapter.page(Source(SESSION, roots), Window())
        assert page.state is HistoryState.unreadable
        assert not page.turns
        assert (
            adapter.since(Source(SESSION, roots), Window(after=0)).state is HistoryState.unreadable
        )
        assert adapter.tally(Source(SESSION, roots)).state is HistoryState.unreadable
        assert adapter.origin(Source(SESSION, roots)) == ""
    assert adapter.page(Source(SESSION, roots), Window()).state is HistoryState.ok


def test_info_never_raises_while_the_store_is_held(roots: list[Path]) -> None:
    """Its docstring promises as much; a title asked mid-write is simply not known yet."""
    named(roots[0] / HERMES_DB, "Plan the migration")
    with held(roots[0] / HERMES_DB):
        assert HermesAdapter().info(Source(SESSION, roots)) == ("", "")
    assert HermesAdapter().info(Source(SESSION, roots))[0] == "Plan the migration"
