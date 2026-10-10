"""The opencode adapter, against two sessions captured verbatim from a real store, in both layouts."""

import json
import sqlite3
from pathlib import Path

import pytest
from pydantic import JsonValue

from herdr_pwa.engine.opencode import (
    OPENCODE_DB,
    OPENCODE_ROOT,
    OPENCODE_STORAGE_ROOT,
    OpencodeAdapter,
    SessionInfo,
    _readonly,  # pyright: ignore[reportPrivateUsage]
)
from herdr_pwa.engine.transcript import (
    MAX_RESULT_CHARS,
    HistoryState,
    ModelRef,
    Speaker,
    TranscriptPage,
    adapter_for,
    history,
    usage,
)
from herdr_pwa.jsontypes import JsonObject, as_object, loads, objects

STORE = Path(__file__).parent / "fixtures" / "transcript" / "opencode"
SESSION = "ses_3fd62ba5bffemIxWVhRda5wgS7"
SUBAGENT = "ses_3fd74bed6ffeiF11LRsQnRVHWJ"


def record(path: Path) -> JsonObject:
    """One captured JSON file, which is always an object."""
    found = as_object(loads(path.read_text()))
    assert found is not None
    return found


def dig(value: JsonValue, *keys: str) -> JsonValue:
    """``value[k1][k2]...``, raising as subscripting would on a missing member."""
    for key in keys:
        holder = as_object(value)
        if holder is None:
            raise TypeError(f"not an object at {key!r}")
        value = holder[key]
    return value


def text(value: JsonValue, *keys: str) -> str:
    """``dig`` for a member the capture always writes as a string."""
    found = dig(value, *keys)
    assert isinstance(found, str)
    return found


def page(*, limit: int = 40, before: int | None = None, after: int | None = None) -> TranscriptPage:
    """One page of the captured session, through the door the app uses."""
    return history(
        SESSION,
        agent_kind="opencode",
        root=STORE,
        limit=limit,
        before=before,
        after=after,
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
        assert isinstance(loads(path.read_text()), dict)


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
    quiet = history(SESSION, agent_kind="cursor", root=STORE)
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
    turns = history(SUBAGENT, agent_kind="opencode", root=STORE, limit=100).turns
    failed = [t for t in turns if t.failed]
    assert len(failed) == 1
    assert failed[0].tool == "webfetch"
    assert "404" in failed[0].result


def test_a_tool_result_is_a_gist_and_says_when_it_was_cut() -> None:
    turns = history(SUBAGENT, agent_kind="opencode", root=STORE, limit=100).turns
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
    got = history(SESSION, agent_kind="opencode", root=store, after=9999)
    assert got.reset
    assert got.turns == []


def test_a_part_still_being_written_stops_the_frontier(store: Path) -> None:
    """Half a JSON file is not a turn, and the cursor must not move past it."""
    stream = sorted((store / "part").rglob("prt_*.json"))
    victim = stream[len(stream) // 2]
    victim.write_text('{"id": "prt_half"')
    got = history(SESSION, agent_kind="opencode", root=store, after=0, limit=100)
    whole = history(SESSION, agent_kind="opencode", root=store, limit=100)
    assert got.end < got.size
    assert len(got.turns) < len(whole.turns)


def test_a_byte_budget_stops_a_page_rather_than_reading_the_store(store: Path) -> None:
    adapter = OpencodeAdapter()
    got = adapter.page(SESSION, roots=[store], limit=100, max_bytes=4096)
    assert got.has_more
    assert len(got.turns) < 23


@pytest.mark.parametrize("hostile", ["../../etc/passwd", "..", ".hidden", "a/b", "a\\b", "", "   "])
def test_a_session_id_that_is_really_a_path_is_refused(hostile: str) -> None:
    got = history(hostile, agent_kind="opencode", root=STORE)
    assert got.state is HistoryState.absent
    assert got.turns == []


def test_a_session_symlinked_out_of_the_root_is_not_followed(store: Path, tmp_path: Path) -> None:
    """A store made of directories must not be walked out of its root."""
    outside = tmp_path / "elsewhere" / "message" / "ses_escape"
    outside.mkdir(parents=True)
    (outside / "msg_x.json").write_text('{"id": "msg_x", "role": "user"}')
    (store / "message" / "ses_escape").symlink_to(outside)
    assert history("ses_escape", agent_kind="opencode", root=store).turns == []


def test_a_store_that_is_not_there_is_absent_not_an_error(tmp_path: Path) -> None:
    got = history(SESSION, agent_kind="opencode", root=tmp_path / "nope")
    assert got.state is HistoryState.absent
    assert got.turns == []


def test_a_session_with_messages_but_no_parts_is_empty(store: Path) -> None:
    empty = store / "message" / "ses_bare"
    empty.mkdir()
    (empty / "msg_bare.json").write_text('{"id": "msg_bare", "role": "user"}')
    got = history("ses_bare", agent_kind="opencode", root=store)
    assert got.state is HistoryState.empty
    assert got.turns == []


def test_a_store_full_of_rubbish_reads_as_no_conversation(store: Path) -> None:
    for path in (store / "part").rglob("*.json"):
        path.write_text("not json at all")
    assert history(SESSION, agent_kind="opencode", root=store, limit=100).turns == []


def test_the_tally_sums_the_messages_the_store_keeps_one_file_each() -> None:
    """A normalised store keys one file per message, so there is nothing to dedupe."""
    tally = usage(SESSION, agent_kind="opencode", root=STORE)
    assert tally.state is HistoryState.ok
    assert tally.messages == 10
    assert tally.input_tokens > 0
    assert tally.output_tokens > 0
    assert tally.models == [ModelRef("moonshotai/Kimi-K2.5", "togetherai")]


def test_the_tally_reports_the_live_window_from_the_newest_message() -> None:
    tally = usage(SESSION, agent_kind="opencode", root=STORE)
    assert 0 < tally.context_tokens < tally.input_tokens


def test_the_tally_of_a_harness_with_no_adapter_is_unsupported() -> None:
    assert usage(SESSION, agent_kind="cursor", root=STORE).state is (HistoryState.unsupported)


def test_a_byte_budget_makes_the_tally_a_floor_rather_than_a_lie(store: Path) -> None:
    tally = OpencodeAdapter().tally(SESSION, roots=[store], max_bytes=512)
    assert tally.partial


# opencode 1.x keeps live sessions in SQLite; schema.sql and db-rows.json are captured verbatim.
SCHEMA = (STORE / "schema.sql").read_text()


def _rows() -> dict[str, list[JsonObject]]:
    found = as_object(loads((STORE / "db-rows.json").read_text()))
    assert found is not None
    return {table: objects(records) for table, records in found.items()}


ROWS = _rows()
PROBE = "ses_fdea0efeaffeE9p6Qfs2Po8KCv"


def build(path: Path, rows: dict[str, list[JsonObject]]) -> Path:
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


def mirrored(rows: dict[str, list[JsonObject]]) -> dict[str, list[JsonObject]]:
    """The 95-file capture as the rows opencode would have written for it."""
    out: dict[str, list[JsonObject]] = dict(rows)
    out["session"] = [
        *out["session"],
        {**rows["session"][0], "id": SESSION, "slug": SESSION, "title": SESSION},
    ]
    messages: list[JsonObject] = []
    parts: list[JsonObject] = []
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
        blob = as_object(loads(str(row["data"])))
        assert blob is not None
        assert "id" not in blob
        assert str(row["id"]).startswith("prt_")


def test_a_turn_driven_into_a_live_opencode_reads_back_out_of_the_database(
    database: Path,
) -> None:
    """The ``ses_`` id herdr reports on the pane is the row key."""
    got = history(PROBE, agent_kind="opencode", root=database, limit=100)
    assert got.state is HistoryState.ok
    assert [(t.speaker, t.text) for t in got.turns] == [
        (Speaker.human, "reply with exactly: roundtrip-probe"),
        (Speaker.agent, "roundtrip-probe"),
    ]


def test_both_layouts_of_the_same_session_read_as_the_same_conversation(
    database: Path,
) -> None:
    """The layout moved; the parser did not: same capture, two stores, one answer."""
    files = history(SESSION, agent_kind="opencode", root=STORE, limit=100)
    rows = history(SESSION, agent_kind="opencode", root=database, limit=100)
    assert rows.size == files.size
    assert [t.model_dump() for t in rows.turns] == [t.model_dump() for t in files.turns]
    assert usage(SESSION, agent_kind="opencode", root=database).model_dump() == (
        usage(SESSION, agent_kind="opencode", root=STORE).model_dump()
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
    turns = history(SESSION, agent_kind="opencode", root=tmp_path, limit=100).turns
    assert {t.text for t in turns} == {"from the database"}


def test_a_root_spelled_either_way_finds_the_frozen_store(tmp_path: Path) -> None:
    """Both the data dir and its ``storage`` child are roots people have configured."""
    for source in STORE.rglob("*.json"):
        target = tmp_path / "storage" / source.relative_to(STORE)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    for root in (tmp_path, tmp_path / "storage"):
        assert history(SESSION, agent_kind="opencode", root=root).state is (HistoryState.ok)


def test_the_database_is_opened_through_a_uri_that_cannot_write(
    database: Path,
) -> None:
    """``mode=ro`` refuses a write at the SQLite layer, not by this module's manners."""
    uri = _readonly(database / OPENCODE_DB)
    assert uri.startswith("file:")
    assert uri.endswith("?mode=ro")
    with sqlite3.connect(uri, uri=True) as db, pytest.raises(sqlite3.OperationalError):
        db.execute("delete from part")


def test_reading_never_touches_the_database_file(database: Path) -> None:
    """Byte-for-byte: the WAL sidecars are SQLite's own, the store itself is not written."""
    path = database / OPENCODE_DB
    before = path.read_bytes()
    history(PROBE, agent_kind="opencode", root=database, limit=100)
    usage(PROBE, agent_kind="opencode", root=database)
    assert path.read_bytes() == before


def test_a_database_that_is_not_opencodes_is_simply_not_history(
    tmp_path: Path,
) -> None:
    """A schema we do not recognise degrades exactly as a missing store does."""
    with sqlite3.connect(tmp_path / OPENCODE_DB) as db:
        db.execute("create table something_else (id text)")
    assert history(PROBE, agent_kind="opencode", root=tmp_path).state is (HistoryState.absent)


def test_a_file_that_is_not_a_database_at_all_is_not_history(tmp_path: Path) -> None:
    (tmp_path / OPENCODE_DB).write_bytes(b"this is not a database")
    assert history(PROBE, agent_kind="opencode", root=tmp_path).state is (HistoryState.absent)
    assert usage(PROBE, agent_kind="opencode", root=tmp_path).state is (HistoryState.absent)


def test_a_session_that_exists_with_no_turns_yet_is_empty_not_absent(
    tmp_path: Path,
) -> None:
    """herdr reports a ``ses_`` id before any turn is written; that is empty, not gone."""
    build(tmp_path / OPENCODE_DB, {"session": ROWS["session"]})
    got = history(PROBE, agent_kind="opencode", root=tmp_path, limit=100)
    assert got.state is HistoryState.empty
    assert got.turns == []
    assert usage(PROBE, agent_kind="opencode", root=tmp_path).state is (HistoryState.empty)


# The session's own record: its title is not in its messages, nor an unanswered session's model.


def test_the_session_row_carries_the_name_opencode_gave_it(database: Path) -> None:
    """The title exists; the same pane's terminal title was the constant ``OpenCode``."""
    info = OpencodeAdapter().info(PROBE, roots=[database])
    assert info.title == "roundtrip-probe"


def test_the_model_column_is_json_and_carries_the_provider_too(
    database: Path,
) -> None:
    """Measured on the live store: ``{"id": ..., "providerID": ...}``, not a name."""
    assert dig(loads(str(ROWS["session"][0]["model"])), "id") == "MiniMaxAI/MiniMax-M3"
    assert OpencodeAdapter().info(PROBE, roots=[database]).model == ModelRef(
        "MiniMaxAI/MiniMax-M3", "togetherai"
    )


def test_a_serving_variant_beside_the_model_is_not_shown_as_one(
    tmp_path: Path,
) -> None:
    """A ``variant`` is a knob on the same model, and the banner has one line to spend."""
    row: JsonObject = {
        **ROWS["session"][0],
        "model": json.dumps(
            {"id": "zai-org/GLM-5.2", "providerID": "togetherai", "variant": "low"}
        ),
    }
    build(tmp_path / OPENCODE_DB, {"session": [row]})
    assert OpencodeAdapter().info(PROBE, roots=[tmp_path]).model == ModelRef(
        "zai-org/GLM-5.2", "togetherai"
    )


def test_an_older_model_column_holding_a_bare_name_still_reads(tmp_path: Path) -> None:
    """The column is not JSON in every build that wrote it, and neither is refused."""
    build(tmp_path / OPENCODE_DB, {"session": [{**ROWS["session"][0], "model": "kimi"}]})
    assert OpencodeAdapter().info(PROBE, roots=[tmp_path]).model == ModelRef("kimi")


def test_a_store_with_no_model_column_still_answers_its_title(tmp_path: Path) -> None:
    """A schema predating the model column narrows the read rather than losing the title."""
    database = tmp_path / OPENCODE_DB
    with sqlite3.connect(database) as db:
        db.execute("create table session (id text primary key, title text)")
        db.execute("insert into session values (?, ?)", (PROBE, "still named"))
    assert OpencodeAdapter().info(PROBE, roots=[tmp_path]) == SessionInfo("still named")


def test_the_frozen_store_answers_the_title_and_has_no_model() -> None:
    """The old layout keeps the title in its own file and never kept a model."""
    info = OpencodeAdapter().info(SESSION, roots=[STORE])
    assert info.title == ("Rewrite the testing notes as an opencode skill")
    assert info.model == ModelRef()


def test_a_session_no_store_holds_has_no_record_and_is_not_an_error(
    tmp_path: Path,
) -> None:
    """A pane with no row anywhere answers absence, not a raise."""
    assert OpencodeAdapter().info(PROBE, roots=[tmp_path]) == SessionInfo()
    assert OpencodeAdapter().info("", roots=[tmp_path]) == SessionInfo()
    assert OpencodeAdapter().info("../../etc/passwd", roots=[STORE]) == SessionInfo()


def test_a_session_with_no_answer_yet_is_still_named_by_its_model(
    tmp_path: Path,
) -> None:
    """No message means no ``modelID``; the row has carried one since creation."""
    build(tmp_path / OPENCODE_DB, {"session": ROWS["session"]})
    tally = usage(PROBE, agent_kind="opencode", root=tmp_path)
    assert tally.messages == 0
    assert tally.models == [ModelRef("MiniMaxAI/MiniMax-M3", "togetherai")]


def test_a_model_a_message_named_is_not_overwritten_by_the_row(
    database: Path,
) -> None:
    """The row fills a gap; it never corrects the model a message says answered."""
    assert usage(SESSION, agent_kind="opencode", root=database).models == (
        [ModelRef("moonshotai/Kimi-K2.5", "togetherai")]
    )


def test_a_byte_budget_bounds_a_page_out_of_the_database_too(
    database: Path,
) -> None:
    whole = OpencodeAdapter().page(SESSION, roots=[database], limit=100)
    clipped = OpencodeAdapter().page(SESSION, roots=[database], limit=100, max_bytes=2048)
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
    got = OpencodeAdapter().since(PROBE, roots=[tmp_path], after=0, limit=100)
    assert got.end < got.size
