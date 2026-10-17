"""The session id herdr reports can name a frozen prefix of the live conversation."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import JsonValue, TypeAdapter

from herdr_pwa.engine import (
    Ask,
    HistoryState,
    Source,
    TailCache,
    UsageCache,
    chat_window,
    locate,
)
from tests.engine_seam import Window, history

FIXTURES = Path(__file__).parent / "fixtures" / "transcript" / "lineage"


def _fixture(name: str) -> dict[str, JsonValue]:
    return TypeAdapter(dict[str, JsonValue]).validate_json((FIXTURES / name).read_text())


FORKED_HEAD = _fixture("forked-head.json")
COMPACTED_HEAD = _fixture("compacted-head.json")
BG_BOUNDARY = _fixture("bg-compact-boundary-2.1.241.json")
BG_STAMP = _fixture("bg-parent-stamp-2.1.241.json")
PLAIN_FORK = _fixture("plain-fork-head-2.1.241.json")


def _forked(head: dict[str, JsonValue]) -> dict[str, JsonValue]:
    parent = head["forkedFrom"]
    assert isinstance(parent, dict)
    return parent


ROOT = "aaaaaaaa-0000-0000-0000-000000000000"
COPY = "bbbbbbbb-0000-0000-0000-000000000000"
LATER = "cccccccc-0000-0000-0000-000000000000"
STRANGER = "dddddddd-0000-0000-0000-000000000000"


def _record(session: str, index: int, ts: str, *, pad: int = 0) -> dict[str, object]:
    """One conversation record, sized to order."""
    return {
        "uuid": f"{session[:8]}-{index:04d}",
        "sessionId": session,
        "type": "user",
        "timestamp": ts,
        "promptSource": "typed",
        "message": {"role": "user", "content": "x" * (40 + pad)},
    }


def _log(  # noqa: PLR0913
    directory: Path,
    session: str,
    *,
    last: str,
    records: int = 3,
    pad: int = 0,
    copied_from: str = "",
    head: dict[str, object] | None = None,
    mtime: float | None = None,
) -> Path:
    """A log for *session* whose newest timestamp is *last*."""
    lines: list[str] = []
    if copied_from:
        # The captured head's shape, re-pointed at this synthetic parent.
        first = dict(head or FORKED_HEAD)
        first["sessionId"] = session
        first["forkedFrom"] = {
            "sessionId": copied_from,
            "messageUuid": str(first.get("uuid", "")),
        }
        lines.append(json.dumps(first))
    lines.extend(
        json.dumps(_record(session, index, "2026-01-01T00:00:00.000Z"))
        for index in range(records - len(lines))
    )
    lines.append(json.dumps(_record(session, 999, last, pad=pad)))
    path = directory / f"{session}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _handoff(  # noqa: PLR0913
    directory: Path,
    session: str,
    *,
    parent: str,
    last: str,
    records: int = 3,
    pad: int = 0,
    background: bool = True,
    stamp: bool = True,
    lead: int = 0,
) -> Path:
    """A 2.1.241 log of the shape a daemon-hosted background handoff writes."""
    boundary = dict(BG_BOUNDARY)
    boundary["sessionId"] = session
    stamped = dict(BG_STAMP)
    stamped["sessionId"] = session
    stamped["session_id"] = parent
    if not background:
        boundary.pop("sessionKind", None)
        stamped.pop("sessionKind", None)
    if not stamp:
        stamped.pop("session_id", None)
    lines = [
        json.dumps({"type": "file-history-snapshot", "snapshot": "s" * 1200}) for _ in range(lead)
    ]
    lines = [json.dumps({"sessionId": session, "type": "ai-title"}), *lines]
    lines += [json.dumps(boundary), json.dumps(stamped)]
    for index in range(max(0, records - len(lines))):
        lines.append(json.dumps(_record(session, index, "2026-01-01T00:00:00.000Z")))
    lines.append(json.dumps(_record(session, 999, last, pad=pad)))
    path = directory / f"{session}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A transcript root with one project directory in it."""
    directory = tmp_path / "projects" / "-home-someone-repo"
    directory.mkdir(parents=True)
    return directory


# The fixture: if these fail, the capture has been edited.


def test_the_captured_heads_really_carry_a_parent_pointer() -> None:
    assert _forked(FORKED_HEAD)["sessionId"] == ("6ae1e031-ddb4-4684-ab07-68073f7fec47")
    assert _forked(COMPACTED_HEAD)["sessionId"] == ("5b609dba-d756-493a-a3fc-b4e09bf0a183")


def test_a_compacted_copy_does_not_share_its_parents_first_uuid() -> None:
    """Why ``forkedFrom`` and not Collie's shared-first-uuid rule."""
    assert COMPACTED_HEAD["subtype"] == "compact_boundary"
    assert COMPACTED_HEAD["uuid"] != _forked(COMPACTED_HEAD)["sessionId"]
    assert COMPACTED_HEAD["uuid"] == _forked(COMPACTED_HEAD)["messageUuid"]


# Following


def test_the_conversation_is_followed_into_the_copy_it_continued_in(
    project: Path,
) -> None:
    """The whole point: herdr names ROOT, the talking happened in COPY."""
    _log(project, ROOT, last="2026-08-20T18:12:00.000Z")
    copy = _log(project, COPY, last="2026-08-20T20:59:00.000Z", records=9, copied_from=ROOT)
    assert locate(ROOT, root=project.parent) == copy


def test_a_chain_of_copies_is_followed_to_its_end(project: Path) -> None:
    """Resume, then resume again. Neither hop is the one herdr knows about."""
    _log(project, ROOT, last="2026-08-20T18:12:00.000Z")
    _log(project, COPY, last="2026-08-20T19:30:00.000Z", records=9, copied_from=ROOT)
    last = _log(project, LATER, last="2026-08-20T20:59:00.000Z", records=14, copied_from=COPY)
    assert locate(ROOT, root=project.parent) == last


def test_a_copy_that_is_not_ours_is_never_considered(project: Path) -> None:
    """A stranger's log in the same directory, newer and larger than ours."""
    named = _log(project, ROOT, last="2026-08-20T18:12:00.000Z")
    _log(
        project,
        STRANGER,
        last="2026-09-01T00:00:00.000Z",
        records=40,
        copied_from="99999999-0000-0000-0000-000000000000",
    )
    assert locate(ROOT, root=project.parent) == named


def test_an_unrelated_log_with_no_parent_at_all_is_never_considered(
    project: Path,
) -> None:
    """Newer, larger, same directory, and simply a different conversation."""
    named = _log(project, ROOT, last="2026-08-20T18:12:00.000Z")
    _log(project, STRANGER, last="2026-09-01T00:00:00.000Z", records=40)
    assert locate(ROOT, root=project.parent) == named


# The two guards. Both were measured to fire on the real lineages on this box.


def test_following_never_shows_less_history_than_not_following(project: Path) -> None:
    """A real copy that went STALE: this is both lineages on this box."""
    named = _log(project, ROOT, last="2026-08-20T20:59:00.000Z", records=20)
    _log(project, COPY, last="2026-07-18T21:29:10.000Z", records=4, copied_from=ROOT)
    assert locate(ROOT, root=project.parent) == named


def test_a_smaller_copy_is_refused_even_when_it_spoke_more_recently(
    project: Path,
) -> None:
    """The size floor is load-bearing on its own, not a corroborator."""
    named = _log(project, ROOT, last="2026-08-20T18:12:00.000Z", records=30, pad=4000)
    _log(project, COPY, last="2026-08-20T20:59:00.000Z", records=3, copied_from=ROOT)
    assert locate(ROOT, root=project.parent) == named


def test_mtime_is_not_what_decides_it(project: Path) -> None:
    """Measured on this box: the mtime order is INVERTED against the content."""
    named = _log(project, ROOT, last="2026-08-18T02:44:39.000Z", records=20, mtime=1_700_000_000)
    _log(
        project,
        COPY,
        last="2026-07-18T21:29:10.000Z",
        records=20,
        copied_from=ROOT,
        mtime=1_800_000_000,
    )
    assert locate(ROOT, root=project.parent) == named


def test_a_copy_that_says_nothing_at_all_cannot_win(project: Path) -> None:
    """No timestamp anywhere in it, so there is nothing to prefer it on."""
    named = _log(project, ROOT, last="2026-08-20T18:12:00.000Z")
    copy = project / f"{COPY}.jsonl"
    head = dict(FORKED_HEAD)
    head["sessionId"] = COPY
    head["forkedFrom"] = {"sessionId": ROOT, "messageUuid": "x"}
    head.pop("timestamp", None)
    copy.write_text(json.dumps(head) + "\n" + "z" * 9000 + "\n", encoding="utf-8")
    assert locate(ROOT, root=project.parent) == named


def test_a_cycle_of_copies_terminates(project: Path) -> None:
    """Nothing on disk is trusted to be a tree, including by us."""
    _log(project, ROOT, last="2026-08-20T18:12:00.000Z", copied_from=COPY)
    _log(project, COPY, last="2026-08-20T19:00:00.000Z", copied_from=ROOT)
    assert locate(ROOT, root=project.parent) == project / f"{COPY}.jsonl"


# Containment, and what the reader above it sees


def test_a_copy_that_is_a_symlink_out_of_the_root_is_not_followed(
    project: Path, tmp_path: Path
) -> None:
    """The containment rule applies to a candidate exactly as it does to the named file."""
    named = _log(project, ROOT, last="2026-08-20T18:12:00.000Z")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    secret = _log(elsewhere, COPY, last="2026-09-01T00:00:00.000Z", records=40)
    secret_body = secret.read_text(encoding="utf-8")
    secret.unlink()
    planted = elsewhere / "secret.jsonl"
    planted.write_text(
        json.dumps(
            {
                "sessionId": COPY,
                "uuid": "s1",
                "type": "user",
                "timestamp": "2026-09-01T00:00:00.000Z",
                "forkedFrom": {"sessionId": ROOT, "messageUuid": "x"},
            }
        )
        + "\n"
        + secret_body,
        encoding="utf-8",
    )
    (project / f"{COPY}.jsonl").symlink_to(planted)
    assert locate(ROOT, root=project.parent) == named


def test_the_chat_reads_the_copy_the_conversation_is_actually_in(
    project: Path,
) -> None:
    """End to end, through ``history`` -- which is what the Chat tab calls."""
    _log(project, ROOT, last="2026-08-20T18:12:00.000Z")
    copy = project / f"{COPY}.jsonl"
    head = dict(FORKED_HEAD)
    head["sessionId"] = COPY
    head["forkedFrom"] = {"sessionId": ROOT, "messageUuid": "x"}
    copy.write_text(
        "\n".join(
            [json.dumps(head)]
            + [
                json.dumps(
                    {
                        "uuid": "c1",
                        "sessionId": COPY,
                        "type": "user",
                        "timestamp": "2026-08-20T20:59:00.000Z",
                        "promptSource": "typed",
                        "message": {"role": "user", "content": "the invisible hours"},
                    }
                )
            ]
            + [json.dumps(_record(COPY, i, "2026-08-20T20:00:00.000Z")) for i in range(9)]
        )
        + "\n",
        encoding="utf-8",
    )
    page = history(Source.of(ROOT, root=project.parent))
    assert page.state is HistoryState.ok
    assert "the invisible hours" in {turn.text for turn in page.turns}


def test_a_held_cursor_names_the_file_it_was_taken_against(project: Path) -> None:
    """A byte offset is a promise about ONE file, and made it say which."""
    named = _log(project, ROOT, last="2026-08-20T18:12:00.000Z")
    copy = _log(project, COPY, last="2026-08-20T20:59:00.000Z", records=9, copied_from=ROOT)
    assert named.exists()
    assert locate(ROOT, root=project.parent) == copy
    honoured = history(
        Source.of(ROOT, root=project.parent, log=copy.stem), Window(after=copy.stat().st_size)
    )
    assert honoured.turns == []
    assert honoured.reset is False
    assert honoured.log == copy.stem

    stale = history(
        Source.of(ROOT, root=project.parent, log=named.stem), Window(after=named.stat().st_size)
    )
    assert stale.reset is True, "a cursor from another file must never be honoured"
    assert stale.turns == []
    assert stale.end == copy.stat().st_size


def test_the_scroll_back_cursor_names_its_file_too(project: Path) -> None:
    """The cursor guard covers ``page`` as well as ``since``."""
    named = _log(project, ROOT, last="2026-08-20T18:12:00.000Z")
    copy = _log(project, COPY, last="2026-08-20T20:59:00.000Z", records=60, copied_from=ROOT)
    assert locate(ROOT, root=project.parent) == copy

    honoured = history(
        Source.of(ROOT, root=project.parent, log=copy.stem), Window(before=copy.stat().st_size)
    )
    assert honoured.reset is False
    assert honoured.moved is False

    stale = history(
        Source.of(ROOT, root=project.parent, log=named.stem), Window(before=named.stat().st_size)
    )
    assert stale.reset is True, "a cursor from another file must never be honoured"
    assert stale.moved is True, "and the reason is a MOVE, not a rewrite"
    # Its own tail, which is what a first read of this file would have answered.
    fresh = history(Source.of(ROOT, root=project.parent))
    assert [t.uuid for t in stale.turns] == [t.uuid for t in fresh.turns]


def test_a_scroll_back_cursor_that_names_no_file_is_trusted_as_it_always_was(
    project: Path,
) -> None:
    """An older client holding a `before` from before still pages upward."""
    named = _log(project, ROOT, last="2026-08-20T18:12:00.000Z", records=60)
    page = history(Source.of(ROOT, root=project.parent), Window(before=named.stat().st_size))
    assert page.reset is False
    assert page.moved is False


def test_a_cursor_that_names_no_file_is_trusted_as_it_always_was(
    project: Path,
) -> None:
    """An older client, and any provider whose pages carry no log, still work."""
    named = _log(project, ROOT, last="2026-08-20T18:12:00.000Z")
    page = history(Source.of(ROOT, root=project.parent), Window(after=named.stat().st_size))
    assert page.reset is False
    assert page.turns == []


# 2.1.241: what the provider stopped writing, and the one thing it still does


def test_the_2_1_241_captures_carry_no_forked_from() -> None:
    """The regression in one assertion: the edge was built on is gone."""
    for head in (BG_BOUNDARY, BG_STAMP, PLAIN_FORK):
        assert head["version"] == "2.1.241"
        assert "forkedFrom" not in head


def test_a_foreground_fork_carries_no_edge_of_any_kind() -> None:
    """The honest limit, captured. This is the ORDINARY case, not a degenerate one."""
    assert "forkedFrom" not in PLAIN_FORK
    assert "session_id" not in PLAIN_FORK
    assert "sessionKind" not in PLAIN_FORK


def test_the_captured_boundary_predates_the_fork_that_copied_it() -> None:
    """Why a compact boundary alone may not waive the size floor (F2)."""
    assert BG_BOUNDARY["subtype"] == "compact_boundary"
    assert str(BG_BOUNDARY["timestamp"]).startswith("2026-08-20")
    assert BG_BOUNDARY["sessionKind"] == "bg"


def test_a_background_child_is_followed_through_its_session_id_stamp(
    project: Path,
) -> None:
    """The fix: parent and child, no ``forkedFrom`` anywhere, child is followed."""
    _log(project, ROOT, last="2026-08-20T18:12:00.000Z")
    child = _handoff(project, COPY, parent=ROOT, last="2026-08-23T01:08:00.000Z", records=9)
    assert locate(ROOT, root=project.parent) == child


def test_a_background_child_is_followed_even_though_it_is_smaller(
    project: Path,
) -> None:
    """The measured shape: 19.2 MB named, 4.0 MB live -- 21% of the parent."""
    named = _log(project, ROOT, last="2026-08-20T18:12:00.000Z", records=40, pad=4000)
    child = _handoff(project, COPY, parent=ROOT, last="2026-08-23T01:08:00.000Z", records=3)
    assert child.stat().st_size < named.stat().st_size
    assert locate(ROOT, root=project.parent) == child


def test_a_smaller_newer_boundary_carrying_foreground_copy_is_still_refused(
    project: Path,
) -> None:
    """The amendment that keeps the floor's own property (F2)."""
    named = _log(project, ROOT, last="2026-08-20T18:12:00.000Z", records=40, pad=4000)
    _handoff(
        project,
        COPY,
        parent=ROOT,
        last="2026-08-23T01:08:00.000Z",
        records=3,
        background=False,
    )
    assert locate(ROOT, root=project.parent) == named


def test_a_smaller_copy_with_no_compact_boundary_is_still_refused(
    project: Path,
) -> None:
    """The containment guard, re-asserted against the new code path rather than inherited."""
    named = _log(project, ROOT, last="2026-08-20T18:12:00.000Z", records=40, pad=4000)
    _log(project, COPY, last="2026-08-23T01:08:00.000Z", records=3, copied_from=ROOT)
    assert locate(ROOT, root=project.parent) == named


def test_a_child_with_no_edge_at_all_is_not_followed(project: Path) -> None:
    """The unfollowable residue, asserted as unfollowable rather than assumed away."""
    named = _log(project, ROOT, last="2026-08-20T18:12:00.000Z")
    _handoff(
        project,
        COPY,
        parent=ROOT,
        last="2026-08-23T01:08:00.000Z",
        records=40,
        stamp=False,
    )
    assert locate(ROOT, root=project.parent) == named


def test_a_foreign_session_id_from_an_unrelated_log_is_never_an_edge(
    project: Path,
) -> None:
    """Containment: a stamp naming a session we never asked about wins nothing."""
    named = _log(project, ROOT, last="2026-08-20T18:12:00.000Z")
    _handoff(
        project,
        STRANGER,
        parent="99999999-0000-0000-0000-000000000000",
        last="2026-09-01T00:00:00.000Z",
        records=40,
    )
    assert locate(ROOT, root=project.parent) == named


def test_a_three_deep_background_lineage_still_resolves(project: Path) -> None:
    """A grandchild's stamp may name the GRANDparent, so the walk is breadth-first."""
    _log(project, ROOT, last="2026-08-20T18:12:00.000Z")
    _handoff(project, COPY, parent=ROOT, last="2026-08-21T00:00:00.000Z", records=9)
    last = _handoff(project, LATER, parent=COPY, last="2026-08-23T01:08:00.000Z", records=14)
    assert locate(ROOT, root=project.parent) == last


def test_an_edge_pushed_past_the_head_window_is_not_followed(project: Path) -> None:
    """The constraint that actually binds is that the record ENDS inside 64 KiB."""
    named = _log(project, ROOT, last="2026-08-20T18:12:00.000Z")
    _handoff(
        project,
        COPY,
        parent=ROOT,
        last="2026-08-23T01:08:00.000Z",
        records=9,
        lead=60,
    )
    assert locate(ROOT, root=project.parent) == named


# A chain: the floor is the file each copy was taken FROM, not the file herdr named

BG_PRESERVED = _fixture("bg-preserved-2.1.283.json")
SIBLING = "eeeeeeee-0000-0000-0000-000000000000"
NEPHEW = "ffffffff-0000-0000-0000-000000000000"


@dataclass(frozen=True)
class _Copy:
    """One log copied out of another, and the newest timestamp it holds."""

    session: str
    parent: str
    last: str


def _onward(directory: Path, copy: _Copy, records: int) -> Path:
    """A background log whose head carries its parent's stamp and no compaction."""
    session, last = copy.session, copy.last
    preserved = dict(BG_PRESERVED)
    preserved["sessionId"] = session
    preserved["session_id"] = copy.parent
    lines = [json.dumps({"sessionId": session, "type": "ai-title"}), json.dumps(preserved)]
    lines += [
        json.dumps(_record(session, index, "2026-01-01T00:00:00.000Z")) for index in range(records)
    ]
    lines.append(json.dumps(_record(session, 999, last)))
    path = directory / f"{session}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _chain(project: Path) -> tuple[Path, Path, Path]:
    """ROOT, handed off compacted to COPY, handed off again uncompacted to LATER."""
    named = _log(project, ROOT, last="2026-08-20T18:12:00.000Z", records=300, pad=4000)
    first = _handoff(project, COPY, parent=ROOT, last="2026-08-21T00:00:00.000Z", records=3)
    second = _onward(project, _Copy(LATER, COPY, "2026-08-23T01:08:00.000Z"), 20)
    assert first.stat().st_size < second.stat().st_size < named.stat().st_size
    return named, first, second


def test_the_captured_onward_record_is_background_and_not_a_boundary() -> None:
    """What makes the second hop need the per-copy floor: nothing in it waives one."""
    assert BG_PRESERVED["sessionKind"] == "bg"
    assert BG_PRESERVED["type"] == "assistant"
    assert "compactMetadata" not in BG_PRESERVED
    assert "isCompactSummary" not in BG_PRESERVED


def test_a_second_handoff_smaller_than_the_named_file_is_followed(project: Path) -> None:
    """Stopping at the first hop leaves the Chat on a file nothing writes any more."""
    _, _, second = _chain(project)
    assert locate(ROOT, root=project.parent) == second


def test_a_sibling_fork_off_the_named_file_is_still_refused_beside_a_chain(
    project: Path,
) -> None:
    """Larger and newer than both hops, but it holds less than the file it was copied from."""
    named, _, second = _chain(project)
    sibling = _log(project, SIBLING, last="2026-08-24T00:00:00.000Z", records=60, copied_from=ROOT)
    assert second.stat().st_size < sibling.stat().st_size < named.stat().st_size
    assert locate(ROOT, root=project.parent) == second


def test_a_refused_copy_lends_nothing_to_the_copies_made_from_it(project: Path) -> None:
    """A refused sibling's child, larger than the sibling, is still down a dead branch."""
    named = _log(project, ROOT, last="2026-08-20T18:12:00.000Z", records=300, pad=4000)
    sibling = _log(project, SIBLING, last="2026-08-21T00:00:00.000Z", copied_from=ROOT)
    nephew = _log(project, NEPHEW, last="2026-08-24T00:00:00.000Z", records=60, copied_from=SIBLING)
    assert sibling.stat().st_size < nephew.stat().st_size < named.stat().st_size
    assert locate(ROOT, root=project.parent) == named


def test_the_chat_and_the_usage_move_when_the_chain_grows(project: Path) -> None:
    """Both caches key on the located path, so they must re-locate rather than hold it."""
    _log(project, ROOT, last="2026-08-20T18:12:00.000Z", records=300, pad=4000)
    first = _handoff(project, COPY, parent=ROOT, last="2026-08-21T00:00:00.000Z", records=3)
    source = Source.of(ROOT, agent_kind="claude", root=project.parent)
    tails, usage = TailCache(), UsageCache()
    assert chat_window(source, ask=Ask(messages=5), tails=tails).log == first.stem
    assert usage.read(source).last_at == "2026-08-21T00:00:00.000Z"
    second = _onward(project, _Copy(LATER, COPY, "2026-08-23T01:08:00.000Z"), 20)
    held = Source.of(ROOT, agent_kind="claude", root=project.parent, log=first.stem)
    moved = chat_window(held, ask=Ask(messages=5), tails=tails)
    assert (moved.log, moved.moved) == (second.stem, True)
    assert usage.read(source).last_at == "2026-08-23T01:08:00.000Z"
