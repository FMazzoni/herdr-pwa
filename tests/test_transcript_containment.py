"""A transcript path is re-checked for containment after it is resolved."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from herdr_pwa.engine import HistoryState, locate
from tests.engine_seam import history

SESSION = "11111111-2222-3333-4444-555555555555"

# A parseable record, so a successful escape would be visibly successful.
OUTSIDE_RECORD = (
    '{"uuid":"a1","type":"user","timestamp":"2026-08-19T00:00:00Z",'
    '"message":{"role":"user","content":"ssh-rsa AAAAB3NzaC1yc2EAAAA"}}\n'
)


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """A transcript-shaped file that is nowhere near the transcript root."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    secret = elsewhere / "secret.jsonl"
    secret.write_text(OUTSIDE_RECORD, encoding="utf-8")
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
    """The project directory is the link, the shape the glob invites."""
    (outside.parent / f"{SESSION}.jsonl").write_text(OUTSIDE_RECORD, encoding="utf-8")
    (root / "-linked-project").symlink_to(outside.parent, target_is_directory=True)
    assert locate(SESSION, root=root) is None


def test_the_escape_is_indistinguishable_from_an_absent_file(root: Path, outside: Path):
    """What the client sees, which is the half that matters at the URL."""
    (root / "-home-someone-repo" / f"{SESSION}.jsonl").symlink_to(outside)
    page = history(SESSION, root=root)
    assert page.state is HistoryState.absent
    assert page.turns == []
    assert "ssh-rsa" not in page.model_dump_json()


def test_a_relative_link_climbing_out_with_dotdot_is_not_followed(root: Path, outside: Path):
    """``..`` inside the link target, which the session id check never sees."""
    link = root / "-home-someone-repo" / f"{SESSION}.jsonl"
    link.symlink_to(os.path.relpath(outside, link.parent))
    assert locate(SESSION, root=root) is None


def test_a_real_file_inside_the_root_is_still_found(root: Path):
    """The other half of every containment fix: it must not break the app."""
    real = root / "-home-someone-repo" / f"{SESSION}.jsonl"
    real.write_text(OUTSIDE_RECORD, encoding="utf-8")
    assert locate(SESSION, root=root) == real


# More than one root, and the containment rule that goes with it


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
    real.write_text(OUTSIDE_RECORD, encoding="utf-8")
    assert locate(SESSION, root=[root]) == real
    assert locate(SESSION, root=str(root)) == real


def test_the_roots_are_searched_in_order(two_roots: tuple[Path, Path]) -> None:
    """First root holding the uuid wins."""
    first, second = two_roots
    wanted = second / "-home-someone-repo" / f"{SESSION}.jsonl"
    wanted.write_text(OUTSIDE_RECORD, encoding="utf-8")
    assert locate(SESSION, root=[first, second]) == wanted
    assert locate(SESSION, root=[second, first]) == wanted


def test_an_empty_entry_is_dropped_rather_than_searched(root: Path) -> None:
    """``''`` resolves relative to the process cwd, which is nobody's tree."""
    real = root / "-home-someone-repo" / f"{SESSION}.jsonl"
    real.write_text(OUTSIDE_RECORD, encoding="utf-8")
    assert locate(SESSION, root=["", str(root), "   "]) == real
    assert locate(SESSION, root=["", "  "]) is None


def test_a_root_that_cannot_be_read_does_not_blank_the_ones_after_it(
    tmp_path: Path, root: Path
) -> None:
    """A missing profile is an ordinary state, not a reason to lose the others."""
    real = root / "-home-someone-repo" / f"{SESSION}.jsonl"
    real.write_text(OUTSIDE_RECORD, encoding="utf-8")
    assert locate(SESSION, root=[tmp_path / "no-such-profile", root]) == real


def test_a_link_planted_in_one_profile_cannot_reach_into_another(
    two_roots: tuple[Path, Path],
) -> None:
    """The containment rule that goes WITH the multi-root search."""
    first, second = two_roots
    target = second / "-home-someone-repo" / f"{SESSION}.jsonl"
    target.write_text(OUTSIDE_RECORD, encoding="utf-8")
    (first / "-home-someone-repo" / f"{SESSION}.jsonl").symlink_to(target)
    # The link is found first and refused; the search goes on to the real file.
    assert locate(SESSION, root=[first, second]) == target
    # And on its own, the planted link reaches nothing at all.
    assert locate(SESSION, root=[first]) is None
