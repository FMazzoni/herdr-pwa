"""The opencode adapter, against two sessions from a real store (content synthetic), in both layouts."""

import json
import sqlite3
from pathlib import Path

import pytest
from pydantic import JsonValue, TypeAdapter

from herdr_pwa.engine import (
    HistoryState,
    ModelRef,
    Source,
    Speaker,
    TranscriptPage,
    is_api_error,
)
from tests._jsonpath import at, obj
from tests.engine_seam import (
    MAX_RESULT_CHARS,
    OPENCODE_DB,
    OPENCODE_ROOT,
    OPENCODE_STORAGE_ROOT,
    OpencodeAdapter,
    SessionInfo,
    Window,
    adapter_for,
    history,
    is_message,
    readonly_uri,
    usage,
)

STORE = Path(__file__).parent / "fixtures" / "transcript" / "opencode"
SESSION = "ses_3fd62ba5bffemIxWVhRda5wgS7"
SUBAGENT = "ses_3fd74bed6ffeiF11LRsQnRVHWJ"


# A captured record as opencode wrote it, every field kept: these hold the capture's shape.
RECORD = TypeAdapter(dict[str, JsonValue])


def record(path: Path) -> dict[str, JsonValue]:
    """One captured JSON file, which is always an object."""
    return RECORD.validate_json(path.read_text())


def dig(value: JsonValue, *keys: str) -> JsonValue:
    """``value[k1][k2]...``, raising as subscripting would on a missing member."""
    return at(value, *keys)


def text(value: JsonValue, *keys: str) -> str:
    """``dig`` for a member the capture always writes as a string."""
    found = dig(value, *keys)
    assert isinstance(found, str)
    return found


def page(*, limit: int = 40, before: int | None = None, after: int | None = None) -> TranscriptPage:
    """One page of the captured session, through the door the app uses."""
    return history(
        Source.of(SESSION, agent_kind="opencode", root=STORE),
        Window(limit=limit, before=before, after=after),
    )


@pytest.fixture
def store(tmp_path: Path) -> Path:
    """A writable copy of the capture, for the tests that damage it."""
    copy = tmp_path / "storage"
    for source in STORE.rglob("*.json"):
        target = copy / source.relative_to(STORE)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    return copy


# If these fail the capture has been edited, and everything below measures something else.


def test_the_capture_is_a_normalised_store_not_a_log() -> None:
    """No line is a record here: messages and their parts are separate files."""
    assert (STORE / "message" / SESSION).is_dir()
    parts = list((STORE / "part").iterdir())
    assert len(parts) > len(list((STORE / "message" / SESSION).iterdir()))
    for path in STORE.rglob("*.json"):
        assert record(path) is not None


def test_the_capture_covers_every_part_type_the_reader_branches_on() -> None:
    kinds = {text(record(p), "type") for p in (STORE / "part").rglob("*.json")}
    assert {"text", "tool", "file", "reasoning", "step-start", "patch"} <= kinds
    states = {
        text(record(p), "state", "status")
        for p in (STORE / "part").rglob("*.json")
        if dig(record(p), "type") == "tool"
    }
    assert {"completed", "error"} == states


def test_message_ids_sort_into_the_order_they_were_written() -> None:
    """The whole frontier rests on this: an id sorts, so a listing is a stream."""
    times = [
        dig(record(p), "time", "created") for p in sorted((STORE / "message" / SESSION).iterdir())
    ]
    assert all(isinstance(t, int) for t in times)
    assert times == sorted(t for t in times if isinstance(t, int))


def test_opencode_is_registered_and_defaults_to_its_own_store() -> None:
    adapter = adapter_for("opencode")
    assert isinstance(adapter, OpencodeAdapter)
    assert adapter.default_roots == (OPENCODE_ROOT,)
    assert OPENCODE_STORAGE_ROOT.startswith(OPENCODE_ROOT)


def test_a_harness_with_no_adapter_still_simply_has_no_history() -> None:
    """cursor keeps no session log at all: no history is a supported state, not an error."""
    quiet = history(Source.of(SESSION, agent_kind="cursor", root=STORE))
    assert adapter_for("cursor") is None
    assert quiet.state is HistoryState.unsupported
    assert quiet.turns == []


def test_the_conversation_comes_out_oldest_first() -> None:
    whole = page(limit=100)
    assert whole.state is HistoryState.ok
    speakers = [t.speaker for t in whole.turns]
    assert speakers[0] is Speaker.human
    assert Speaker.agent in speakers
    assert Speaker.tool in speakers


def test_a_typed_prompt_is_the_person_and_synthetic_feedback_is_not() -> None:
    """opencode writes tool feedback as ``synthetic`` user text, which a person never said."""
    turns = page(limit=100).turns
    human = [t.text for t in turns if t.speaker is Speaker.human]
    system = [t.text for t in turns if t.speaker is Speaker.system]
    assert any(t.startswith("look up how the tests in this repo are run") for t in human)
    assert not any(t.startswith("Called the Read tool") for t in human)
    assert any(t.startswith("Called the Read tool") for t in system)


def test_a_tool_call_arrives_with_its_result_already_on_it() -> None:
    """One part carries input AND output, so nothing has to be paired up."""
    tools = [t for t in page(limit=100).turns if t.speaker is Speaker.tool]
    write = next(t for t in tools if t.tool == "write")
    assert write.text.endswith("notes/testing.md")
    assert write.result.startswith("Wrote file")
    assert write.call
    assert not write.failed


def test_a_failed_tool_carries_the_error_as_its_result() -> None:
    turns = history(Source.of(SUBAGENT, agent_kind="opencode", root=STORE), Window(limit=100)).turns
    failed = [t for t in turns if t.failed]
    assert len(failed) == 1
    assert failed[0].tool == "webfetch"
    assert "404" in failed[0].result


def test_a_tool_result_is_a_gist_and_says_when_it_was_cut() -> None:
    turns = history(Source.of(SUBAGENT, agent_kind="opencode", root=STORE), Window(limit=100)).turns
    cut = [t for t in turns if t.result_truncated]
    assert cut, "the capture's webfetch outputs are long enough to be clipped"
    assert all(len(t.result) <= MAX_RESULT_CHARS for t in turns)


def test_the_machinery_around_the_conversation_is_not_rendered() -> None:
    """``step-start``, ``step-finish``, ``patch`` and ``reasoning`` are machinery, not lines."""
    whole = page(limit=100)
    assert whole.size == 47
    assert len(whole.turns) == 23


# The frontier is a position in the part stream, not a byte offset.


def test_paging_backwards_walks_the_whole_session_exactly_once() -> None:
    whole = page(limit=100).turns
    seen: list[str] = []
    before: int | None = None
    while True:
        got = page(limit=4, before=before)
        seen = [t.uuid for t in got.turns] + seen
        if not got.has_more:
            break
        before = got.cursor
    assert seen == [t.uuid for t in whole]


def test_handing_the_end_back_asks_for_what_was_appended() -> None:
    whole = page(limit=100)
    quiet = page(after=whole.end, limit=100)
    assert quiet.turns == []
    assert quiet.state is HistoryState.empty
    assert not quiet.has_more
    everything = page(after=0, limit=100)
    assert [t.uuid for t in everything.turns] == [t.uuid for t in whole.turns]


def test_a_frontier_past_the_end_of_the_store_is_a_reset(store: Path) -> None:
    """The store shrank under a held cursor, so the view must re-read rather than splice."""
    got = history(Source.of(SESSION, agent_kind="opencode", root=store), Window(after=9999))
    assert got.reset
    assert got.turns == []


def test_a_part_still_being_written_stops_the_frontier(store: Path) -> None:
    """Half a JSON file is not a turn, and the cursor must not move past it."""
    stream = sorted((store / "part").rglob("prt_*.json"))
    victim = stream[len(stream) // 2]
    victim.write_text('{"id": "prt_half"')
    got = history(Source.of(SESSION, agent_kind="opencode", root=store), Window(after=0, limit=100))
    whole = history(Source.of(SESSION, agent_kind="opencode", root=store), Window(limit=100))
    assert got.end < got.size
    assert len(got.turns) < len(whole.turns)


def test_a_byte_budget_stops_a_page_rather_than_reading_the_store(store: Path) -> None:
    adapter = OpencodeAdapter()
    got = adapter.page(Source(SESSION, [store]), Window(limit=100, max_bytes=4096))
    assert got.has_more
    assert len(got.turns) < 23


@pytest.mark.parametrize("hostile", ["../../etc/passwd", "..", ".hidden", "a/b", "a\\b", "", "   "])
def test_a_session_id_that_is_really_a_path_is_refused(hostile: str) -> None:
    got = history(Source.of(hostile, agent_kind="opencode", root=STORE))
    assert got.state is HistoryState.absent
    assert got.turns == []


def test_a_session_symlinked_out_of_the_root_is_not_followed(store: Path, tmp_path: Path) -> None:
    """A store made of directories must not be walked out of its root."""
    outside = tmp_path / "elsewhere" / "message" / "ses_escape"
    outside.mkdir(parents=True)
    (outside / "msg_x.json").write_text('{"id": "msg_x", "role": "user"}')
    (store / "message" / "ses_escape").symlink_to(outside)
    assert history(Source.of("ses_escape", agent_kind="opencode", root=store)).turns == []


def test_a_store_that_is_not_there_is_absent_not_an_error(tmp_path: Path) -> None:
    got = history(Source.of(SESSION, agent_kind="opencode", root=tmp_path / "nope"))
    assert got.state is HistoryState.absent
    assert got.turns == []


def test_a_session_with_messages_but_no_parts_is_empty(store: Path) -> None:
    empty = store / "message" / "ses_bare"
    empty.mkdir()
    (empty / "msg_bare.json").write_text('{"id": "msg_bare", "role": "user"}')
    got = history(Source.of("ses_bare", agent_kind="opencode", root=store))
    assert got.state is HistoryState.empty
    assert got.turns == []


def test_a_store_full_of_rubbish_reads_as_no_conversation(store: Path) -> None:
    for path in (store / "part").rglob("*.json"):
        path.write_text("not json at all")
    assert (
        history(Source.of(SESSION, agent_kind="opencode", root=store), Window(limit=100)).turns
        == []
    )


def test_the_tally_sums_the_messages_the_store_keeps_one_file_each() -> None:
    """A normalised store keys one file per message, so there is nothing to dedupe."""
    tally = usage(Source.of(SESSION, agent_kind="opencode", root=STORE))
    assert tally.state is HistoryState.ok
    assert tally.messages == 10
    assert tally.input_tokens > 0
    assert tally.output_tokens > 0
    assert tally.models == [ModelRef("moonshotai/Kimi-K2.5", "togetherai")]


def test_the_tally_reports_the_live_window_from_the_newest_message() -> None:
    tally = usage(Source.of(SESSION, agent_kind="opencode", root=STORE))
    assert 0 < tally.context_tokens < tally.input_tokens


def test_the_tally_of_a_harness_with_no_adapter_is_unsupported() -> None:
    assert usage(Source.of(SESSION, agent_kind="cursor", root=STORE)).state is (
        HistoryState.unsupported
    )


def test_a_byte_budget_makes_the_tally_a_floor_rather_than_a_lie(store: Path) -> None:
    tally = OpencodeAdapter().tally(Source(SESSION, [store]), max_bytes=512)
    assert tally.partial


# opencode 1.x keeps live sessions in SQLite; schema.sql is as captured, db-rows.json a probe's rows.
SCHEMA = (STORE / "schema.sql").read_text()


def _rows() -> dict[str, list[dict[str, JsonValue]]]:
    return TypeAdapter(dict[str, list[dict[str, JsonValue]]]).validate_json(
        (STORE / "db-rows.json").read_text()
    )


ROWS = _rows()
PROBE = "ses_fdea0efeaffeE9p6Qfs2Po8KCv"


def build(path: Path, rows: dict[str, list[dict[str, JsonValue]]]) -> Path:
    """A database with opencode's own schema, holding exactly *rows*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        for table, records in rows.items():
            for row in records:
                columns = ", ".join(f'"{name}"' for name in row)
                marks = ", ".join("?" for _ in row)
                db.execute(
                    f"insert into {table} ({columns}) values ({marks})",  # noqa: S608
                    tuple(row.values()),
                )
    return path


def mirrored(rows: dict[str, list[dict[str, JsonValue]]]) -> dict[str, list[dict[str, JsonValue]]]:
    """The 95-file capture as the rows opencode would have written for it."""
    out: dict[str, list[dict[str, JsonValue]]] = dict(rows)
    out["session"] = [
        *out["session"],
        {**rows["session"][0], "id": SESSION, "slug": SESSION, "title": SESSION},
    ]
    messages: list[dict[str, JsonValue]] = []
    parts: list[dict[str, JsonValue]] = []
    for path in sorted((STORE / "message" / SESSION).iterdir()):
        message = record(path)
        stamp = dig(message, "time", "created")
        messages.append(
            {
                "id": message["id"],
                "session_id": SESSION,
                "time_created": stamp,
                "time_updated": stamp,
                # The db keeps the ids as columns, so the blob is the file without them.
                "data": json.dumps(
                    {k: v for k, v in message.items() if k not in ("id", "sessionID")}
                ),
            }
        )
        for part in sorted((STORE / "part" / str(message["id"])).iterdir()):
            got = record(part)
            parts.append(
                {
                    "id": got["id"],
                    "message_id": message["id"],
                    "session_id": SESSION,
                    "time_created": stamp,
                    "time_updated": stamp,
                    "data": json.dumps(
                        {k: v for k, v in got.items() if k not in ("id", "sessionID", "messageID")}
                    ),
                }
            )
    out["message"] = [*out["message"], *messages]
    out["part"] = [*out["part"], *parts]
    return out


@pytest.fixture
def database(tmp_path: Path) -> Path:
    """A data root holding only the SQLite store, as opencode 1.x leaves it."""
    build(tmp_path / OPENCODE_DB, mirrored(ROWS))
    return tmp_path


def test_the_captured_rows_keep_their_ids_in_columns_not_in_the_blob() -> None:
    """The file layout carries the ids inside the record and the table does not."""
    for row in ROWS["part"]:
        assert "id" not in RECORD.validate_json(str(row["data"]))
        assert str(row["id"]).startswith("prt_")


def test_a_turn_driven_into_a_live_opencode_reads_back_out_of_the_database(
    database: Path,
) -> None:
    """The ``ses_`` id herdr reports on the pane is the row key."""
    got = history(Source.of(PROBE, agent_kind="opencode", root=database), Window(limit=100))
    assert got.state is HistoryState.ok
    assert [(t.speaker, t.text) for t in got.turns] == [
        (Speaker.human, "reply with exactly: roundtrip-probe"),
        (Speaker.agent, "roundtrip-probe"),
    ]


def test_both_layouts_of_the_same_session_read_as_the_same_conversation(
    database: Path,
) -> None:
    """The layout moved; the parser did not: same capture, two stores, one answer."""
    files = history(Source.of(SESSION, agent_kind="opencode", root=STORE), Window(limit=100))
    rows = history(Source.of(SESSION, agent_kind="opencode", root=database), Window(limit=100))
    assert rows.size == files.size
    assert [t.model_dump() for t in rows.turns] == [t.model_dump() for t in files.turns]
    assert usage(Source.of(SESSION, agent_kind="opencode", root=database)).model_dump() == (
        usage(Source.of(SESSION, agent_kind="opencode", root=STORE)).model_dump()
    )


def test_the_live_store_wins_over_the_frozen_one_when_both_hold_it(
    tmp_path: Path,
) -> None:
    """storage/ is read, but never in preference to the database opencode is writing now."""
    for source in STORE.rglob("*.json"):
        target = tmp_path / "storage" / source.relative_to(STORE)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    rows = mirrored(ROWS)
    rows["part"] = [
        r
        if r["session_id"] != SESSION
        else {**r, "data": json.dumps({"type": "text", "text": "from the database"})}
        for r in rows["part"]
    ]
    build(tmp_path / OPENCODE_DB, rows)
    turns = history(
        Source.of(SESSION, agent_kind="opencode", root=tmp_path), Window(limit=100)
    ).turns
    assert {t.text for t in turns} == {"from the database"}


def test_a_root_spelled_either_way_finds_the_frozen_store(tmp_path: Path) -> None:
    """Both the data dir and its ``storage`` child are roots people have configured."""
    for source in STORE.rglob("*.json"):
        target = tmp_path / "storage" / source.relative_to(STORE)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    for root in (tmp_path, tmp_path / "storage"):
        assert history(Source.of(SESSION, agent_kind="opencode", root=root)).state is (
            HistoryState.ok
        )


def test_the_database_is_opened_through_a_uri_that_cannot_write(
    database: Path,
) -> None:
    """``mode=ro`` refuses a write at the SQLite layer, not by this module's manners."""
    uri = readonly_uri(database / OPENCODE_DB)
    assert uri.startswith("file:")
    assert uri.endswith("?mode=ro")
    with sqlite3.connect(uri, uri=True) as db, pytest.raises(sqlite3.OperationalError):
        db.execute("delete from part")


def test_reading_never_touches_the_database_file(database: Path) -> None:
    """Byte-for-byte: the WAL sidecars are SQLite's own, the store itself is not written."""
    path = database / OPENCODE_DB
    before = path.read_bytes()
    history(Source.of(PROBE, agent_kind="opencode", root=database), Window(limit=100))
    usage(Source.of(PROBE, agent_kind="opencode", root=database))
    assert path.read_bytes() == before


def test_a_database_that_is_not_opencodes_is_simply_not_history(
    tmp_path: Path,
) -> None:
    """A schema we do not recognise degrades exactly as a missing store does."""
    with sqlite3.connect(tmp_path / OPENCODE_DB) as db:
        db.execute("create table something_else (id text)")
    assert history(Source.of(PROBE, agent_kind="opencode", root=tmp_path)).state is (
        HistoryState.absent
    )


def test_a_file_that_is_not_a_database_at_all_is_not_history(tmp_path: Path) -> None:
    (tmp_path / OPENCODE_DB).write_bytes(b"this is not a database")
    assert history(Source.of(PROBE, agent_kind="opencode", root=tmp_path)).state is (
        HistoryState.absent
    )
    assert usage(Source.of(PROBE, agent_kind="opencode", root=tmp_path)).state is (
        HistoryState.absent
    )


def test_a_session_that_exists_with_no_turns_yet_is_empty_not_absent(
    tmp_path: Path,
) -> None:
    """herdr reports a ``ses_`` id before any turn is written; that is empty, not gone."""
    build(tmp_path / OPENCODE_DB, {"session": ROWS["session"]})
    got = history(Source.of(PROBE, agent_kind="opencode", root=tmp_path), Window(limit=100))
    assert got.state is HistoryState.empty
    assert got.turns == []
    assert usage(Source.of(PROBE, agent_kind="opencode", root=tmp_path)).state is (
        HistoryState.empty
    )


# The session's own record: its title is not in its messages, nor an unanswered session's model.


def test_the_session_row_carries_the_name_opencode_gave_it(database: Path) -> None:
    """The title exists; the same pane's terminal title was the constant ``OpenCode``."""
    info = OpencodeAdapter().info(Source(PROBE, [database]))
    assert info.title == "roundtrip-probe"


def test_the_model_column_is_json_and_carries_the_provider_too(
    database: Path,
) -> None:
    """Measured on the live store: ``{"id": ..., "providerID": ...}``, not a name."""
    assert (
        dig(RECORD.validate_json(str(ROWS["session"][0]["model"])), "id") == "MiniMaxAI/MiniMax-M3"
    )
    assert OpencodeAdapter().info(Source(PROBE, [database])).model == ModelRef(
        "MiniMaxAI/MiniMax-M3", "togetherai"
    )


def test_a_serving_variant_beside_the_model_is_not_shown_as_one(
    tmp_path: Path,
) -> None:
    """A ``variant`` is a knob on the same model, and the banner has one line to spend."""
    row: dict[str, JsonValue] = {
        **ROWS["session"][0],
        "model": json.dumps(
            {"id": "zai-org/GLM-5.2", "providerID": "togetherai", "variant": "low"}
        ),
    }
    build(tmp_path / OPENCODE_DB, {"session": [row]})
    assert OpencodeAdapter().info(Source(PROBE, [tmp_path])).model == ModelRef(
        "zai-org/GLM-5.2", "togetherai"
    )


def test_an_older_model_column_holding_a_bare_name_still_reads(tmp_path: Path) -> None:
    """The column is not JSON in every build that wrote it, and neither is refused."""
    build(tmp_path / OPENCODE_DB, {"session": [{**ROWS["session"][0], "model": "kimi"}]})
    assert OpencodeAdapter().info(Source(PROBE, [tmp_path])).model == ModelRef("kimi")


def test_a_store_with_no_model_column_still_answers_its_title(tmp_path: Path) -> None:
    """A schema predating the model column narrows the read rather than losing the title."""
    database = tmp_path / OPENCODE_DB
    with sqlite3.connect(database) as db:
        db.execute("create table session (id text primary key, title text)")
        db.execute("insert into session values (?, ?)", (PROBE, "still named"))
    assert OpencodeAdapter().info(Source(PROBE, [tmp_path])) == SessionInfo("still named")


def test_the_frozen_store_answers_the_title_and_has_no_model() -> None:
    """The old layout keeps the title in its own file and never kept a model."""
    info = OpencodeAdapter().info(Source(SESSION, [STORE]))
    assert info.title == "Rewrite the testing notes as an opencode skill"
    assert info.model == ModelRef()


def test_a_session_no_store_holds_has_no_record_and_is_not_an_error(
    tmp_path: Path,
) -> None:
    """A pane with no row anywhere answers absence, not a raise."""
    assert OpencodeAdapter().info(Source(PROBE, [tmp_path])) == SessionInfo()
    assert OpencodeAdapter().info(Source("", [tmp_path])) == SessionInfo()
    assert OpencodeAdapter().info(Source("../../etc/passwd", [STORE])) == SessionInfo()


def test_a_session_with_no_answer_yet_is_still_named_by_its_model(
    tmp_path: Path,
) -> None:
    """No message means no ``modelID``; the row has carried one since creation."""
    build(tmp_path / OPENCODE_DB, {"session": ROWS["session"]})
    tally = usage(Source.of(PROBE, agent_kind="opencode", root=tmp_path))
    assert tally.messages == 0
    assert tally.models == [ModelRef("MiniMaxAI/MiniMax-M3", "togetherai")]


def test_a_model_a_message_named_is_not_overwritten_by_the_row(
    database: Path,
) -> None:
    """The row fills a gap; it never corrects the model a message says answered."""
    assert usage(Source.of(SESSION, agent_kind="opencode", root=database)).models == (
        [ModelRef("moonshotai/Kimi-K2.5", "togetherai")]
    )


def test_a_byte_budget_bounds_a_page_out_of_the_database_too(
    database: Path,
) -> None:
    whole = OpencodeAdapter().page(Source(SESSION, [database]), Window(limit=100))
    clipped = OpencodeAdapter().page(Source(SESSION, [database]), Window(limit=100, max_bytes=2048))
    assert clipped.has_more
    assert 0 < len(clipped.turns) < len(whole.turns)


def test_a_row_whose_blob_is_rubbish_stops_the_forward_frontier(
    tmp_path: Path,
) -> None:
    """A record that does not parse is not stepped over, it is waited for."""
    rows = mirrored(ROWS)
    rows["part"] = [
        r if r["id"] != rows["part"][2]["id"] else {**r, "data": "not json at all"}
        for r in rows["part"]
    ]
    build(tmp_path / OPENCODE_DB, rows)
    got = OpencodeAdapter().since(Source(PROBE, [tmp_path]), Window(after=0, limit=100))
    assert got.end < got.size


def test_a_locked_store_reads_as_unreadable_not_empty(database: Path) -> None:
    """A writer holding the store must not read as "nothing said yet"."""
    path = database / OPENCODE_DB
    holder = sqlite3.connect(path)
    # An exclusive transaction blocks every other connection, readers included.
    holder.execute("begin exclusive")
    try:
        page = history(Source.of(PROBE, agent_kind="opencode", root=database))
        assert page.state is HistoryState.unreadable
        assert not page.turns
        assert (
            usage(Source.of(PROBE, agent_kind="opencode", root=database)).state
            is HistoryState.unreadable
        )
    finally:
        holder.rollback()
        holder.close()


def test_a_locked_store_reads_fine_once_the_writer_is_gone(database: Path) -> None:
    """The lock is momentary; the next read is the honest page, not a stale refusal."""
    holder = sqlite3.connect(database / OPENCODE_DB)
    holder.execute("begin exclusive")
    try:
        assert (
            history(Source.of(PROBE, agent_kind="opencode", root=database)).state
            is HistoryState.unreadable
        )
    finally:
        holder.rollback()
        holder.close()
    assert history(Source.of(PROBE, agent_kind="opencode", root=database)).state is HistoryState.ok


def test_info_never_raises_while_the_store_is_held(database: Path) -> None:
    """Its docstring promises as much; a name asked mid-write is simply not known yet."""
    holder = sqlite3.connect(database / OPENCODE_DB)
    holder.execute("begin exclusive")
    try:
        assert OpencodeAdapter().info(Source(PROBE, [database])) == SessionInfo()
        assert OpencodeAdapter().origin(Source(PROBE, [database])) == ""
    finally:
        holder.rollback()
        holder.close()
    assert OpencodeAdapter().info(Source(PROBE, [database])).title


# ---- spend per model -------------------------------------------------------------------------

# Real rows from one session that switched models, paths replaced, plus rows borrowed from two
# other sessions: a model whose provider records nothing, and a free one that records no cost.
MIXED = "ses_0dfb24cf5ffemJcBmqyE29sVBi"
MIXED_ROWS = TypeAdapter(dict[str, list[dict[str, JsonValue]]]).validate_json(
    (STORE / "per-model-rows.json").read_text()
)


def spend_of(rows: dict[str, list[dict[str, JsonValue]]], root: Path) -> list[tuple[object, ...]]:
    """The tally's per-model rows, as plain tuples a literal can be compared with."""
    build(root / OPENCODE_DB, rows)
    tally = usage(Source.of(MIXED, agent_kind="opencode", root=root))
    return [
        (
            row.ref,
            row.messages,
            row.input_tokens,
            row.output_tokens,
            row.thinking_tokens,
            row.cache_read_tokens,
            row.cache_creation_tokens,
            round(row.cost, 8),
        )
        for row in tally.by_model
    ]


def edited(index: int, old: str, new: str) -> dict[str, list[dict[str, JsonValue]]]:
    """The mixed rows with one message's stored record changed by a text replacement."""
    messages = [dict(row) for row in MIXED_ROWS["message"]]
    messages[index]["data"] = str(messages[index]["data"]).replace(old, new)
    return {**MIXED_ROWS, "message": messages}


def test_the_tally_splits_spend_by_provider_and_model_in_first_seen_order(
    tmp_path: Path,
) -> None:
    assert spend_of(MIXED_ROWS, tmp_path) == [
        (ModelRef("zai-org/GLM-5.2", "togetherai"), 4, 14381, 1475, 2814, 39872, 0, 0.04937172),
        (ModelRef("MiniMaxAI/MiniMax-M3", "togetherai"), 2, 150367, 1117, 0, 150528, 0, 0.05548218),
        (ModelRef("zai-org/GLM-5.3-Flash", "togetherai"), 2, 0, 0, 0, 0, 0, 0.0),
        (ModelRef("hy3-free", "opencode"), 1, 254, 3, 11, 8448, 0, 0.0),
    ]


def test_the_per_model_rows_add_up_to_the_session_total(tmp_path: Path) -> None:
    build(tmp_path / OPENCODE_DB, MIXED_ROWS)
    tally = usage(Source.of(MIXED, agent_kind="opencode", root=tmp_path))
    assert sum(row.messages for row in tally.by_model) == tally.messages == 9
    assert sum(row.input_tokens for row in tally.by_model) == tally.input_tokens
    assert sum(row.cache_read_tokens for row in tally.by_model) == tally.cache_read_tokens
    assert sum(row.cost for row in tally.by_model) == pytest.approx(tally.cost)


def test_a_model_whose_provider_records_nothing_is_not_counted(tmp_path: Path) -> None:
    """All zeros is the provider saying nothing, which is not the same as using nothing."""
    build(tmp_path / OPENCODE_DB, MIXED_ROWS)
    tally = usage(Source.of(MIXED, agent_kind="opencode", root=tmp_path))
    assert [(row.ref.model, row.recorded) for row in tally.by_model] == [
        ("zai-org/GLM-5.2", True),
        ("MiniMaxAI/MiniMax-M3", True),
        ("zai-org/GLM-5.3-Flash", False),
        ("hy3-free", True),
    ]


def test_the_same_model_from_another_provider_is_another_row(tmp_path: Path) -> None:
    """A model id alone does not name who served it, or what they charged."""
    rows = edited(2, '"providerID":"togetherai"', '"providerID":"fireworks"')
    assert [ref for ref, *_ in spend_of(rows, tmp_path)][:3] == [
        ModelRef("zai-org/GLM-5.2", "togetherai"),
        ModelRef("zai-org/GLM-5.2", "fireworks"),
        ModelRef("MiniMaxAI/MiniMax-M3", "togetherai"),
    ]


def test_cache_written_is_kept_apart_from_cache_read(tmp_path: Path) -> None:
    """No captured row writes the cache, so one is made to: the two must not be folded."""
    glm = spend_of(edited(1, '"write":0', '"write":321'), tmp_path)[0]
    assert glm[5:7] == (39872, 321)


# ---- API errors and interrupts ---------------------------------------------------------------

# opencode keeps a failed answer as the message's own `error`, often with no part at all. One
# real session that hit a provider's rate limit, then borrowed rows: two interrupts out of the
# live store (one before anything was written, one mid tool call) and, out of the frozen store,
# a prompt that outgrew the model, a request the provider refused, and an interrupt worded the
# older way. Paths, prompts, a command and request ids replaced by neutral ones.
ERRORED = "ses_fdb61ad3cffeYvgWpZTc6KTDX2"
ERROR_ROWS = TypeAdapter(dict[str, list[dict[str, JsonValue]]]).validate_json(
    (STORE / "error-rows.json").read_text()
)
RATE_LIMITED = "Too many requests in a short window."
OVERFLOW = "Requested token count exceeds the model's maximum context length"
CONTEXT_LINE = "Full · run /compact or start a new session"


def _error_of(row: dict[str, JsonValue]) -> dict[str, JsonValue] | None:
    found = RECORD.validate_json(str(row["data"])).get("error")
    return found if isinstance(found, dict) else None


def errored_page(
    root: Path, rows: dict[str, list[dict[str, JsonValue]]] = ERROR_ROWS
) -> TranscriptPage:
    """The whole errored session, out of a database holding *rows*."""
    build(root / OPENCODE_DB, rows)
    return history(Source.of(ERRORED, agent_kind="opencode", root=root), Window(limit=400))


def notices(got: TranscriptPage) -> list[tuple[str, str, bool]]:
    return [(t.tool, t.text, t.failed) for t in got.turns if t.speaker is Speaker.notice]


def test_the_fixture_holds_every_error_shape_found_locally() -> None:
    errors = [e for e in map(_error_of, ERROR_ROWS["message"]) if e is not None]
    shapes = sorted((str(e["name"]), obj(e, "data").get("statusCode")) for e in errors)
    assert shapes == [
        ("APIError", 400),
        ("APIError", 400),
        ("APIError", 429),
        ("MessageAbortedError", None),
        ("MessageAbortedError", None),
        ("MessageAbortedError", None),
    ]
    # Measured: every errored row records no tokens and no money.
    for row in ERROR_ROWS["message"]:
        if _error_of(row) is not None:
            data = RECORD.validate_json(str(row["data"]))
            assert data["cost"] == 0
            assert dig(data, "tokens", "input") == dig(data, "tokens", "output") == 0


def test_every_failed_answer_is_a_quiet_line_where_it_happened(tmp_path: Path) -> None:
    got = errored_page(tmp_path)
    speakers = [t.speaker for t in got.turns]
    # The rate limit had no part at all, yet stands between the prompt it refused and the next.
    assert speakers[:3] == [Speaker.human, Speaker.notice, Speaker.human]
    assert len(notices(got)) == 6
    assert not any(t.speaker is Speaker.agent and not t.text for t in got.turns)


def test_a_rate_limit_says_to_send_again_and_opens_on_the_providers_words(tmp_path: Path) -> None:
    turn = next(t for t in errored_page(tmp_path).turns if t.tool == "Limit")
    assert (turn.text, turn.failed) == ("Rate limited · wait, then send it again", True)
    assert turn.result.startswith(RATE_LIMITED)


def test_a_prompt_that_outgrew_the_model_says_to_compact(tmp_path: Path) -> None:
    turn = next(t for t in errored_page(tmp_path).turns if t.tool == "Context")
    assert (turn.text, turn.failed) == (CONTEXT_LINE, True)
    assert turn.result.startswith(OVERFLOW)


def test_any_other_error_is_its_own_text_with_nothing_more_to_open(tmp_path: Path) -> None:
    turn = next(t for t in errored_page(tmp_path).turns if t.tool == "Error")
    assert (turn.text, turn.result, turn.failed) == ("Input validation error", "", True)


def test_an_interrupt_is_one_still_line_and_not_a_failure(tmp_path: Path) -> None:
    """The person stopped it: said once, quietly, whichever way opencode worded it."""
    got = errored_page(tmp_path)
    stopped = [t for t in got.turns if t.tool == "Interrupted"]
    assert [(t.text, t.result, t.failed) for t in stopped] == [("", "", False)] * 3
    # The interrupt mid tool call follows the call it cut off, not the other way round.
    cut = got.turns.index(stopped[1])
    assert got.turns[cut - 1].speaker is Speaker.tool
    assert "User aborted the command" in got.turns[cut - 1].result


def test_errors_are_harness_lines_not_messages(tmp_path: Path) -> None:
    got = errored_page(tmp_path)
    assert len(notices(got)) == 6
    for turn in got.turns:
        if turn.speaker is Speaker.notice:
            assert is_api_error(turn)
            assert not is_message(turn)


def test_paging_backwards_walks_the_errored_session_exactly_once(tmp_path: Path) -> None:
    whole = errored_page(tmp_path)
    assert len(notices(whole)) == 6
    source = Source.of(ERRORED, agent_kind="opencode", root=tmp_path)
    seen: list[str] = []
    before: int | None = None
    while True:
        got = history(source, Window(limit=3, before=before))
        seen = [t.uuid for t in got.turns] + seen
        if not got.has_more:
            break
        before = got.cursor
    assert seen == [t.uuid for t in whole.turns]


def test_an_error_written_onto_a_finished_turn_arrives_as_appended(tmp_path: Path) -> None:
    """opencode writes the error when the answer ends, after its parts: a poll must see it."""
    rows = {**ERROR_ROWS, "message": [dict(r) for r in ERROR_ROWS["message"]]}
    last = rows["message"][-1]
    final = str(last["data"])
    clean = RECORD.validate_json(final)
    clean.pop("error")
    last["data"] = json.dumps(clean)
    before = errored_page(tmp_path / "before", rows)
    last["data"] = final
    build(tmp_path / "after" / OPENCODE_DB, rows)
    source = Source.of(ERRORED, agent_kind="opencode", root=tmp_path / "after")
    grown = history(source, Window(after=before.end, limit=400))
    assert [(t.tool, t.speaker) for t in grown.turns] == [("Interrupted", Speaker.notice)]


def _as_files(rows: dict[str, list[dict[str, JsonValue]]], base: Path) -> Path:
    """*rows* written the way the frozen layout keeps them: one JSON file per record."""
    for message in rows["message"]:
        record = {
            "id": message["id"],
            "sessionID": ERRORED,
            **RECORD.validate_json(str(message["data"])),
        }
        path = base / "message" / ERRORED / f"{message['id']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record))
    for part in rows["part"]:
        record = {
            "id": part["id"],
            "messageID": part["message_id"],
            **RECORD.validate_json(str(part["data"])),
        }
        path = base / "part" / str(part["message_id"]) / f"{part['id']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record))
    return base


def test_both_layouts_read_the_same_errors(tmp_path: Path) -> None:
    live = errored_page(tmp_path / "live")
    frozen = _as_files(ERROR_ROWS, tmp_path / "frozen" / "storage")
    got = history(Source.of(ERRORED, agent_kind="opencode", root=frozen), Window(limit=400))
    assert len(notices(live)) == 6
    assert notices(got) == notices(live)
    assert [t.uuid for t in got.turns] == [t.uuid for t in live.turns]


def test_a_failed_answer_spends_nothing_and_names_no_model(tmp_path: Path) -> None:
    build(tmp_path / OPENCODE_DB, ERROR_ROWS)
    tally = usage(Source.of(ERRORED, agent_kind="opencode", root=tmp_path))
    # Three answers came back; six failed. The frozen rows' GLM-4.7 never answered here.
    assert tally.messages == 3
    assert tally.models == [ModelRef("MiniMaxAI/MiniMax-M3", "togetherai")]
    assert [row.messages for row in tally.by_model] == [3]


def test_an_interrupt_after_an_answer_keeps_that_answers_context(tmp_path: Path) -> None:
    """A failed answer sent nothing up, so the window is still what the last answer sent."""
    build(tmp_path / "plain" / OPENCODE_DB, MIXED_ROWS)
    plain = usage(Source.of(MIXED, agent_kind="opencode", root=tmp_path / "plain"))
    interrupt = next(
        r for r in ERROR_ROWS["message"] if _error_of(r) is not None and "Aborted" in str(r["data"])
    )
    late = {**interrupt, "id": "msg_ffff00001001Borrowed0Fixture", "session_id": MIXED}
    build(tmp_path / "cut" / OPENCODE_DB, {**MIXED_ROWS, "message": [*MIXED_ROWS["message"], late]})
    cut = usage(Source.of(MIXED, agent_kind="opencode", root=tmp_path / "cut"))
    assert plain.context_tokens > 0
    assert (cut.context_tokens, cut.messages) == (plain.context_tokens, plain.messages)
