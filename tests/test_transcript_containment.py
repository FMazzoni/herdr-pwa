"""A transcript path is re-checked AFTER it is resolved.

PRIOR ART: Collie's ``bridge/journal/files.ts`` ``containedRealpath``, which
treats "the one place the bridge touches the filesystem" as a place to pin shut
rather than a place to be careful in.

``engine.transcript.locate`` validates the *session id* -- no ``/``, no ``\\``,
no leading ``.`` -- and then globs ``*/<id>.jsonl`` under the transcript root.
It used to return whatever the glob found. A symlink inside that tree was
followed, so a file or a project directory pointing anywhere on the disk was
read and rendered into the chat, over a URL HANDOFF item 9 says to treat as a
root login.

**The rule** is not "compare the strings we were handed" -- a symlink pointing
anywhere satisfies that. It is: resolve the path, then re-check containment
against *the root it was resolved through*, never against a union of roots.
That last clause is not idle: it is what keeps the multi-root search safe,
where a link planted in one profile must not be able to reach into another.

**The answer** to a containment failure is indistinguishable, to anything the
client can see, from an absent file -- both are ``HistoryState.absent``. A
refusal that read differently would tell a prober that the path exists and is
merely out of bounds.

HISTORY, KEPT BECAUSE THE PROCESS FAILURE OUTLIVES THE BUG
----------------------------------------------------------
These five tests were written first by an agent who could not
edit ``engine/transcript.py`` because it belonged to someone else that wave.
The four escapes were marked ``xfail(strict=True)`` -- recording the hole while
it was open, and failing the moment it closed -- with the fix written out in
this docstring and a handover noted on the ticket.

Nobody took the handover. The work was then closed as done, citing a hardening
commit that contained none of this, and the hole stayed open behind a green
suite for as long as nobody ran ``-rX``. A strict xfail is a good handover: it
cannot be left behind once the work is done. It is a bad *alarm*: it is silent
in exactly the report everyone reads.

The markers are gone because the fix landed. If a future change
reopens the hole, these fail as ordinary failures, which is the point.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from herdr_pwa.engine.transcript import HistoryState, history, locate

SESSION = "11111111-2222-3333-4444-555555555555"

#: One valid Claude Code record, so a successful escape is visibly successful:
#: the assertions below would also pass against an unparseable file, and "the
#: reader could not make sense of the secret" is not the property being tested.
SECRET_RECORD = (
    '{"uuid":"a1","type":"user","timestamp":"2026-08-19T00:00:00Z",'
    '"message":{"role":"user","content":"ssh-rsa AAAAB3NzaC1yc2EAAAA"}}\n'
)


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """A transcript-shaped file that is nowhere near the transcript root."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    secret = elsewhere / "secret.jsonl"
    secret.write_text(SECRET_RECORD, encoding="utf-8")
    return secret


@pytest.fixture
def root(tmp_path: Path) -> Path:
    base = tmp_path / "projects"
    (base / "-home-someone-repo").mkdir(parents=True)
    return base


def test_a_symlinked_log_is_not_followed_out_of_the_root(root: Path, outside: Path):
    """The file itself is a link. The name inside the root looks perfect."""
    (root / "-home-someone-repo" / f"{SESSION}.jsonl").symlink_to(outside)
    assert locate(SESSION, root=root) is None


def test_a_symlinked_project_directory_is_not_followed(root: Path, outside: Path):
    """The *directory* is the link, which is the shape the glob invites: the
    session id is untouched and every path component is one we wrote."""
    (outside.parent / f"{SESSION}.jsonl").write_text(SECRET_RECORD, encoding="utf-8")
    (root / "-linked-project").symlink_to(outside.parent, target_is_directory=True)
    assert locate(SESSION, root=root) is None


def test_the_escape_is_indistinguishable_from_an_absent_file(root: Path, outside: Path):
    """What the client sees, which is the half that matters at the URL.

    A refusal that reads differently from "no transcript" tells a prober that
    the path exists and is merely out of bounds.
    """
    (root / "-home-someone-repo" / f"{SESSION}.jsonl").symlink_to(outside)
    page = history(SESSION, root=root)
    assert page.state is HistoryState.absent
    assert page.turns == []
    assert "ssh-rsa" not in page.model_dump_json()


def test_a_relative_link_climbing_out_with_dotdot_is_not_followed(
    root: Path, outside: Path
):
    """``..`` inside the link target, not inside the session id -- the session id
    check never sees it."""
    link = root / "-home-someone-repo" / f"{SESSION}.jsonl"
    link.symlink_to(os.path.relpath(outside, link.parent))
    assert locate(SESSION, root=root) is None


def test_a_real_file_inside_the_root_is_still_found(root: Path):
    """The other half of every containment fix: it must not break the app.

    A containment check that refuses everything passes all four escapes above
    and is still wrong. This is the test that says so.
    """
    real = root / "-home-someone-repo" / f"{SESSION}.jsonl"
    real.write_text(SECRET_RECORD, encoding="utf-8")
    assert locate(SESSION, root=root) == real


# ---------------------------------------------------------------------------
# More than one root, and the containment rule that goes with it
# ---------------------------------------------------------------------------


@pytest.fixture
def two_roots(tmp_path: Path) -> tuple[Path, Path]:
    """Two Claude homes, the shape CLAUDE_CONFIG_DIR gives each profile."""
    first = tmp_path / "profile-a" / "projects"
    second = tmp_path / "profile-b" / "projects"
    for base in (first, second):
        (base / "-home-someone-repo").mkdir(parents=True)
    return first, second


def test_a_single_root_still_works_as_a_one_element_list(root: Path) -> None:
    """The other half of every widening: it must not break the narrow case."""
    real = root / "-home-someone-repo" / f"{SESSION}.jsonl"
    real.write_text(SECRET_RECORD, encoding="utf-8")
    assert locate(SESSION, root=[root]) == real
    assert locate(SESSION, root=str(root)) == real


def test_the_roots_are_searched_in_order(two_roots: tuple[Path, Path]) -> None:
    """First root holding the uuid wins.

    Not a heuristic: session uuids are globally unique, so two roots cannot
    disagree about who one belongs to. It is a lookup, and no profile detection
    exists or is needed.
    """
    first, second = two_roots
    wanted = second / "-home-someone-repo" / f"{SESSION}.jsonl"
    wanted.write_text(SECRET_RECORD, encoding="utf-8")
    assert locate(SESSION, root=[first, second]) == wanted
    assert locate(SESSION, root=[second, first]) == wanted


def test_an_empty_entry_is_dropped_rather_than_searched(root: Path) -> None:
    """``''`` resolves relative to the process cwd, which is nobody's tree."""
    real = root / "-home-someone-repo" / f"{SESSION}.jsonl"
    real.write_text(SECRET_RECORD, encoding="utf-8")
    assert locate(SESSION, root=["", str(root), "   "]) == real
    assert locate(SESSION, root=["", "  "]) is None


def test_a_root_that_cannot_be_read_does_not_blank_the_ones_after_it(
    tmp_path: Path, root: Path
) -> None:
    """A missing profile is an ordinary state, not a reason to lose the others."""
    real = root / "-home-someone-repo" / f"{SESSION}.jsonl"
    real.write_text(SECRET_RECORD, encoding="utf-8")
    assert locate(SESSION, root=[tmp_path / "no-such-profile", root]) == real


def test_a_link_planted_in_one_profile_cannot_reach_into_another(
    two_roots: tuple[Path, Path],
) -> None:
    """The containment rule that goes WITH the multi-root search.

    The root that produced a hit is remembered with it, and everything after
    resolution stays inside THAT root rather than a union of the configured
    ones. Otherwise the first profile anyone can write to becomes a key to
    every other one.
    """
    first, second = two_roots
    target = second / "-home-someone-repo" / f"{SESSION}.jsonl"
    target.write_text(SECRET_RECORD, encoding="utf-8")
    (first / "-home-someone-repo" / f"{SESSION}.jsonl").symlink_to(target)
    # The link is found first and refused; the search then goes on and finds
    # the real file in the root it actually lives in.
    assert locate(SESSION, root=[first, second]) == target
    # And on its own, the planted link reaches nothing at all.
    assert locate(SESSION, root=[first]) is None


def test_the_setting_is_a_list() -> None:
    """The env-facing half: one string, comma-separated, empties dropped."""
    from herdr_pwa.settings import UiSettings

    assert UiSettings(transcript_root=" /a , ,/b ").transcript_roots == ("/a", "/b")
    assert UiSettings().transcript_roots == ()
    assert UiSettings(transcript_root="/only").transcript_roots == ("/only",)
