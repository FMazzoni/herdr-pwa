"""The session id herdr reports can name a frozen prefix.

PRIOR ART: Collie's ``bridge/journal/claude.ts:245`` (``conversationRoot``) and
``:344`` (``followContinuation``). ``.agents/docs/collie-comparison.md`` calls
this the single most valuable line-level find in the comparison, and it is the
one fact in there we did not have: Claude Code does **not** keep one file per
conversation. ``/fork``, and resuming past a ``/compact``, COPY the whole thread
into a fresh ``<new-uuid>.jsonl`` and continue there, while herdr goes on
reporting whichever id the agent last announced. Collie watched herdr name a log
frozen at 18:12 while the conversation ran on in another file until 20:59.

MEASURED HERE FIRST, 2026-08-20, and it corrects the ticket's own plan. 52 logs
under ``~/.claude/projects`` at depth 1, every record indexed:

* **Two** lineages, both with the parent still on disk --
  ``6ae1e031 -> 6f5c4c0d`` (193 records copied, a ``/fork``) and
  ``5b609dba -> b0613bb6`` (1939 copied, a resume past ``/compact``).
* Each copy carries an explicit **``forkedFrom": {"sessionId", "messageUuid"}``**
  on its first record. It is a provider-written child->parent pointer: exact,
  directional, and nothing has to be guessed from a shared prefix.
* Collie's rule -- group logs by the uuid of their first record -- finds only
  ONE of the two. ``b0613bb6``'s first record is a *new* ``compact_boundary``
  uuid, not the copied one, so first-uuid matching misses that lineage
  entirely. Half the copies on this box, and the guessier half.
* A third rotation exists and leaves no usable link: a background job
  (``sessionKind: "bg"``) opens a new log whose first ``parentUuid`` resolves
  to no record in any other file. Nothing here can follow it, and nothing here
  pretends to.
* **In BOTH lineages the parent is the live file and the copy is stale** -- by a
  month, and a third smaller. So on this box the correct answer is always to
  refuse to follow, and the two guards are the whole feature rather than a
  safety net around it.

WHAT WOULD MAKE THIS WRONG is following the wrong file: reading somebody else's
conversation into this user's chat is worse than showing a stale one. So the
rule is fail-closed: follow only on an explicit ``forkedFrom`` edge,
only to a candidate that is BOTH newer by its last in-file timestamp AND at
least as large, and otherwise serve the file herdr named.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from herdr_pwa.engine.transcript import HistoryState, history, locate

FIXTURES = Path(__file__).parent / "fixtures" / "transcript" / "lineage"
FORKED_HEAD = json.loads((FIXTURES / "forked-head.json").read_text())
COMPACTED_HEAD = json.loads((FIXTURES / "compacted-head.json").read_text())
BG_BOUNDARY = json.loads((FIXTURES / "bg-compact-boundary-2.1.241.json").read_text())
BG_STAMP = json.loads((FIXTURES / "bg-parent-stamp-2.1.241.json").read_text())
PLAIN_FORK = json.loads((FIXTURES / "plain-fork-head-2.1.241.json").read_text())

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


def _log(
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
        # The real captured head, re-pointed at this synthetic parent. The
        # SHAPE is the fixture's; only the ids are ours.
        first = dict(head or FORKED_HEAD)
        first["sessionId"] = session
        first["forkedFrom"] = {
            "sessionId": copied_from,
            "messageUuid": str(first.get("uuid", "")),
        }
        lines.append(json.dumps(first))
    for index in range(records - len(lines)):
        lines.append(json.dumps(_record(session, index, "2026-01-01T00:00:00.000Z")))
    lines.append(json.dumps(_record(session, 999, last, pad=pad)))
    path = directory / f"{session}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _handoff(
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
    """A 2.1.241 log of the shape a daemon-hosted background handoff writes.

    No ``forkedFrom`` anywhere -- 2.1.241 stopped writing it. The parent is
    declared the only way that release declares one: a ``session_id`` stamp on
    the copied-in records, which only a background session writes. *lead* pads
    the head with the ``file-history-snapshot`` records that sit ahead of the
    edge on a real log, so a test can push the edge out of the head window.
    """
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
    """A transcript root with one project directory in it.

    Nested under ``projects/`` so that ``tmp_path`` has room for somewhere the
    root does NOT reach -- which is the whole subject of the containment test.
    """
    directory = tmp_path / "projects" / "-home-someone-repo"
    directory.mkdir(parents=True)
    return directory


# ---------------------------------------------------------------------------
# The fixture. If these fail the capture has been edited and every claim above
# it is measuring something other than what it says.
# ---------------------------------------------------------------------------


def test_the_captured_heads_really_carry_a_parent_pointer() -> None:
    assert FORKED_HEAD["forkedFrom"]["sessionId"] == ("6ae1e031-ddb4-4684-ab07-68073f7fec47")
    assert COMPACTED_HEAD["forkedFrom"]["sessionId"] == ("5b609dba-d756-493a-a3fc-b4e09bf0a183")


def test_a_compacted_copy_does_not_share_its_parents_first_uuid() -> None:
    """Why ``forkedFrom`` and not Collie's shared-first-uuid rule.

    The copy's first record is the ``compact_boundary`` the fork was taken at,
    carrying its OWN new uuid. Grouping logs by first uuid would never place
    this file in its parent's lineage -- and this is one of the only two
    lineages that exist on this box.
    """
    assert COMPACTED_HEAD["subtype"] == "compact_boundary"
    assert COMPACTED_HEAD["uuid"] != COMPACTED_HEAD["forkedFrom"]["sessionId"]
    assert COMPACTED_HEAD["uuid"] == COMPACTED_HEAD["forkedFrom"]["messageUuid"]


# ---------------------------------------------------------------------------
# Following
# ---------------------------------------------------------------------------


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
    """A stranger's log in the same directory, newer and larger than ours.

    It declares a parent we have never heard of, so it is not a candidate --
    reading somebody else's conversation into this chat is the failure this
    whole feature is written to avoid.
    """
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


# ---------------------------------------------------------------------------
# The two guards. Both were measured to fire on the real lineages on this box.
# ---------------------------------------------------------------------------


def test_following_never_shows_less_history_than_not_following(project: Path) -> None:
    """A real copy that went STALE: this is both lineages on this box.

    ``6ae1e031`` was forked into ``6f5c4c0d``, and then the *parent* went on
    being the live conversation for another month while the fork sat at a third
    the size. Following the edge would hand the reader a shorter, older thread.
    """
    named = _log(project, ROOT, last="2026-08-20T20:59:00.000Z", records=20)
    _log(project, COPY, last="2026-07-18T21:29:10.000Z", records=4, copied_from=ROOT)
    assert locate(ROOT, root=project.parent) == named


def test_a_smaller_copy_is_refused_even_when_it_spoke_more_recently(
    project: Path,
) -> None:
    """The size floor is load-bearing on its own, not a corroborator.

    A newer last-timestamp on a file holding a THIRD of the records still means
    less conversation. Both guards must pass, not either.
    """
    named = _log(project, ROOT, last="2026-08-20T18:12:00.000Z", records=30, pad=4000)
    _log(project, COPY, last="2026-08-20T20:59:00.000Z", records=3, copied_from=ROOT)
    assert locate(ROOT, root=project.parent) == named


def test_mtime_is_not_what_decides_it(project: Path) -> None:
    """Measured on this box: the mtime order is INVERTED against the content.

    ``6f5c4c0d`` has the NEWER mtime and holds a conversation a month OLDER --
    something touched it (a ``--resume`` that opened it and closed it) without
    its thread being the live one. Collie's stated preference, "the most
    recently written", picks the wrong file here. The last timestamp INSIDE the
    file is what actually answers "which branch is still being talked in", and
    it is immune to a touch.
    """
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


# ---------------------------------------------------------------------------
# Containment, and what the reader above it sees
# ---------------------------------------------------------------------------


def test_a_copy_that_is_a_symlink_out_of_the_root_is_not_followed(
    project: Path, tmp_path: Path
) -> None:
    """The containment rule applies to a candidate exactly as it does to the named file.

    The head record is inside the root and says everything we want to hear; the
    file it names is a link to somewhere else entirely.
    """
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
    page = history(ROOT, root=project.parent)
    assert page.state is HistoryState.ok
    assert "the invisible hours" in {turn.text for turn in page.turns}


def test_a_held_cursor_names_the_file_it_was_taken_against(project: Path) -> None:
    """A byte offset is a promise about ONE file, and the cursor now says which.

    The ``after`` read follows the lineage, because the page read does: once a
    fork is followed the cursor the client holds was taken against the child,
    and reading it against the parent would splice two threads. So the cursor
    travels with the stem of the file it came from, and a read that resolves a
    different file drops it and answers with the reset it already had for a
    rewritten file -- one honest note, then live growth against the right file.
    """
    named = _log(project, ROOT, last="2026-08-20T18:12:00.000Z")
    copy = _log(project, COPY, last="2026-08-20T20:59:00.000Z", records=9, copied_from=ROOT)
    assert locate(ROOT, root=project.parent, follow=False) == named
    honoured = history(ROOT, root=project.parent, after=copy.stat().st_size, log=copy.stem)
    assert honoured.turns == []
    assert honoured.reset is False
    assert honoured.log == copy.stem

    stale = history(ROOT, root=project.parent, after=named.stat().st_size, log=named.stem)
    assert stale.reset is True, "a cursor from another file must never be honoured"
    assert stale.turns == []
    assert stale.end == copy.stat().st_size


def test_the_SCROLL_BACK_cursor_names_its_file_too(project: Path) -> None:
    """The other half. The guard reached ``since`` and not ``page``.

    The hazard an addendum named -- "splicing fifteen megabytes of the
    parent's old conversation into the child's thread and presenting the result
    as one conversation" -- arrives from BOTH ends of the log, and only the
    bottom one was closed. A ``before`` offset is worse than a refused one, not
    better: ``read_page`` CLAMPS it to the file it lands in, so a stale parent
    offset does not read out of range, it returns the CHILD's NEWEST turns --
    which the top sentinel then prepends above the parent's history as the
    oldest thing on the screen. Recent conversation drawn as ancient, spliced
    across two threads, with nothing saying so.

    The answer is the page this file would have given with no cursor at all --
    its own tail -- and a page that says the cursor was dropped and WHY.
    """
    named = _log(project, ROOT, last="2026-08-20T18:12:00.000Z")
    copy = _log(project, COPY, last="2026-08-20T20:59:00.000Z", records=60, copied_from=ROOT)
    assert locate(ROOT, root=project.parent) == copy

    honoured = history(ROOT, root=project.parent, before=copy.stat().st_size, log=copy.stem)
    assert honoured.reset is False
    assert honoured.moved is False

    stale = history(ROOT, root=project.parent, before=named.stat().st_size, log=named.stem)
    assert stale.reset is True, "a cursor from another file must never be honoured"
    assert stale.moved is True, "and the reason is a MOVE, not a rewrite"
    # Its own tail, which is what a first read of this file would have answered.
    fresh = history(ROOT, root=project.parent)
    assert [t.uuid for t in stale.turns] == [t.uuid for t in fresh.turns]


def test_a_scroll_back_cursor_that_names_no_file_is_trusted_as_it_always_was(
    project: Path,
) -> None:
    """An older client holding a `before` from before the cursor named its file still pages upward."""
    named = _log(project, ROOT, last="2026-08-20T18:12:00.000Z", records=60)
    page = history(ROOT, root=project.parent, before=named.stat().st_size)
    assert page.reset is False
    assert page.moved is False


def test_a_cursor_that_names_no_file_is_trusted_as_it_always_was(
    project: Path,
) -> None:
    """An older client, and any provider whose pages carry no log, still work."""
    named = _log(project, ROOT, last="2026-08-20T18:12:00.000Z")
    page = history(ROOT, root=project.parent, after=named.stat().st_size)
    assert page.reset is False
    assert page.turns == []


# ---------------------------------------------------------------------------
# 2.1.241: what the provider stopped writing, and the one thing it still does
# (as measured, then corrected)
# ---------------------------------------------------------------------------


def test_the_2_1_241_captures_carry_no_forked_from() -> None:
    """The regression in one assertion: the edge the floor was built on is gone.

    All three captures are 2.1.241. None has ``forkedFrom``. The 2.1.214
    fixtures above still do, which is exactly why the suite stayed green while
    the product was broken -- a frozen fixture cannot detect a format change.
    """
    for head in (BG_BOUNDARY, BG_STAMP, PLAIN_FORK):
        assert head["version"] == "2.1.241"
        assert "forkedFrom" not in head


def test_a_foreground_fork_carries_no_edge_of_any_kind() -> None:
    """The honest limit, captured. This is the ORDINARY case, not a degenerate one.

    ``plain-fork-head-2.1.241.json`` is the head of a real ``--fork-session``
    child that holds its parent's whole conversation copied in verbatim. It has
    no ``forkedFrom``, no ``session_id`` and no ``sessionKind``. Nothing can
    follow it. That residue is why ``herdr_pwa.stall`` ships first and alone.
    """
    assert "forkedFrom" not in PLAIN_FORK
    assert "session_id" not in PLAIN_FORK
    assert "sessionKind" not in PLAIN_FORK


def test_the_captured_boundary_predates_the_fork_that_copied_it() -> None:
    """Why a compact boundary alone may not waive the size floor.

    The incident child's first record IS its parent's compaction boundary,
    stamped 2026-08-20 -- three days before the fork happened on 2026-08-23. It
    is small because the resume began at the parent's last compaction point, not
    because anything compacted at handoff, and EVERY fork of that parent taken
    after that date copies the same marker into its head.
    """
    assert BG_BOUNDARY["subtype"] == "compact_boundary"
    assert BG_BOUNDARY["timestamp"].startswith("2026-08-20")
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
    """The measured shape: 19.2 MB named, 4.0 MB live -- 21% of the parent.

    A background handoff resumes at the parent's last compaction point, so it
    is legitimately smaller than its parent forever. The floor cannot tell
    that from an abandoned fork, so the waiver is what makes the live file
    reachable -- and the corroborator below is what keeps it narrow.
    """
    named = _log(project, ROOT, last="2026-08-20T18:12:00.000Z", records=40, pad=4000)
    child = _handoff(project, COPY, parent=ROOT, last="2026-08-23T01:08:00.000Z", records=3)
    assert child.stat().st_size < named.stat().st_size
    assert locate(ROOT, root=project.parent) == child


def test_a_smaller_newer_boundary_carrying_FOREGROUND_copy_is_still_refused(
    project: Path,
) -> None:
    """The amendment that keeps the floor's own property.

    Same boundary, same stamp, same recency, same shrinkage -- and no
    ``sessionKind: "bg"``. The hazard the floor guards against is a person
    forking, looking around and going back to the parent, and that produces a
    FOREGROUND session. Without this, the timestamp gate would be the only guard
    left, and the busiest pair on this box is one quiet day away from that.
    """
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
    """The containment guard, re-asserted against the NEW code path rather than inherited."""
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
    """The constraint that actually binds is that the record ENDS inside 64 KiB.

    On the incident file the edge fits with 20 KB to spare, entirely because
    that log carries 22 ``file-history-snapshot`` records ahead of it where 97
    of 134 transcripts carry one. Pad past that and the edge is unreadable. It
    must fail CLOSED -- serve the named file, no crash, no wrong follow -- and
    the detection half is what makes the resulting freeze honest.
    """
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
