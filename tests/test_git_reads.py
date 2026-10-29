"""The explorer's git reads give git's own answers: status, the file list, diffs and the layout."""

import logging
import os
from pathlib import Path
from typing import Final

import pygit2
import pytest

from herdr_pwa.files import diffs
from herdr_pwa.files import git as git_module
from herdr_pwa.files.git import Git
from tests.filefakes import git, make_repo, trapped_submodule
from tests.journal import journal_text

# git's own reading of a work tree, the oracle every answer here is compared with.
STATUS: Final = (
    "--no-optional-locks",
    "status",
    "--porcelain=v1",
    "-z",
    "-b",
    "--untracked-files=all",
    "--ignore-submodules=all",
)
LISTED: Final = ("ls-files", "-z", "--cached", "--others", "--exclude-standard")


def _porcelain(repo: Path) -> tuple[str, list[tuple[str, str]]]:
    fields = git(repo, *STATUS).decode().split("\0")
    branch = ""
    changes: list[tuple[str, str]] = []
    skip = False
    for item in fields:
        if skip:
            skip = False
        elif item.startswith("## "):
            branch = item[3:].split("...", 1)[0]
        elif item:
            changes.append((item[3:], item[:2]))
            skip = "R" in item[:2] or "C" in item[:2]
    return branch, sorted(changes)


def _git_diff(repo: Path, path: str) -> str:
    has_head = bool(git(repo, "for-each-ref", "--count=1", "refs/heads"))
    base = "HEAD" if has_head else "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
    flags = ("--no-ext-diff", "--no-textconv", "--no-color", "--ignore-submodules=all", "-M")
    return git(repo, "--no-optional-locks", "diff", *flags, base, "--", path).decode()


def _same_as_git(repo: Path) -> None:
    status = Git().status(repo)
    assert (status.branch, sorted((c.path, c.code) for c in status.changes)) == _porcelain(repo)
    listed = {n for n in git(repo, *LISTED).decode().split("\0") if n}
    assert Git().files(repo) == listed
    for change in status.changes:
        ours = Git().diff(repo, change.path)
        assert ours is not None, change.path
        assert diffs.parse(ours) == diffs.parse(_git_diff(repo, change.path)), change.path


def _write(repo: Path, rel: str, body: str | bytes) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(body, bytes):
        path.write_bytes(body)
    else:
        path.write_text(body)


def _busy(repo: Path) -> Path:
    """The fixture repository with every kind of change git status reports, at once."""
    make_repo(repo)
    _write(repo, "docs/naïve plan.md", "# plan\n\nfirst\n")
    git(repo, "add", "-A", "docs")
    git(repo, "commit", "-q", "-m", "plan")
    git(repo, "mv", "docs/guide.md", "docs/handbook.md")
    _write(repo, "src/app/__init__.py", "X = 1\n")
    git(repo, "add", "src/app/__init__.py")
    _write(repo, "src/app/__init__.py", "X = 2\n")
    _write(repo, "staged-then-gone.txt", "brief\n")
    git(repo, "add", "staged-then-gone.txt")
    (repo / "staged-then-gone.txt").unlink()
    git(repo, "rm", "-q", "logs/big.log")
    _write(repo, "docs/naïve plan.md", "# plan\n\nsecond\n")
    _write(repo, "data/blob.bin", b"\x00\x09changed\x00" * 64)
    (repo / "assets/logo.png").unlink()
    (repo / "assets/logo.png").symlink_to("../README.md")
    _write(repo, "notes/deep/a.txt", "deep\n")
    _write(repo, "build/more.txt", "still ignored\n")
    return repo


def test_status_files_and_diffs_match_git_with_every_kind_of_change(tmp_path: Path) -> None:
    repo = _busy(tmp_path / "busy")
    git(repo, "branch", "other")
    git(repo, "config", "branch.main.remote", ".")
    git(repo, "config", "branch.main.merge", "refs/heads/other")
    codes = {c.code for c in Git().status(repo).changes}
    assert {" M", " D", "??", "R ", "MM", "AD", "D ", " T"} <= codes
    _same_as_git(repo)


def test_a_repository_with_no_commit_yet_matches_git(tmp_path: Path) -> None:
    repo = tmp_path / "fresh"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    _write(repo, "staged.txt", "one\ntwo\n")
    git(repo, "add", "staged.txt")
    _write(repo, "loose.txt", "three\n")
    status = Git().status(repo)
    assert status.branch == "No commits yet on main"
    assert [(c.path, c.kind) for c in status.changes] == [
        ("loose.txt", "new"),
        ("staged.txt", "added"),
    ]
    _same_as_git(repo)


def test_a_detached_head_matches_git(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "detached")
    git(repo, "checkout", "-q", "--detach")
    assert Git().status(repo).branch == "HEAD (no branch)"
    _same_as_git(repo)


def test_a_merge_conflict_matches_git(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "conflicted")
    git(repo, "stash", "-q", "-u")
    git(repo, "checkout", "-q", "-b", "theirs")
    _write(repo, "README.md", "# theirs\n")
    _write(repo, "both.txt", "theirs\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "theirs")
    git(repo, "checkout", "-q", "main")
    _write(repo, "README.md", "# ours\n")
    _write(repo, "both.txt", "ours\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "ours")
    with pytest.raises(Exception, match="returned non-zero"):
        git(repo, "merge", "-q", "theirs")
    status = Git().status(repo)
    assert [(c.path, c.code, c.kind) for c in status.changes] == [
        ("README.md", "UU", "conflict"),
        ("both.txt", "AA", "added"),
    ]
    _same_as_git(repo)


def test_an_untracked_repository_inside_matches_git(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "outer")
    inner = repo / "nested"
    inner.mkdir()
    git(inner, "init", "-q", "-b", "main")
    _write(inner, "inside.txt", "x\n")
    _same_as_git(repo)


def test_a_changed_submodule_is_not_a_change_but_is_a_file(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "super")
    trapped_submodule(repo, tmp_path / "marks")
    status = Git().status(repo)
    assert status.under("vendor") == 0
    assert status.of("vendor") is None
    assert "vendor" in Git().files(repo)
    assert Git().diff(repo, "vendor") == ""
    _same_as_git(repo)


def test_the_top_is_found_from_inside_and_nothing_outside_a_repository(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "work" / "repo")
    assert Git().toplevel(repo / "src" / "app") == repo
    assert Git().toplevel(repo) == repo
    assert Git().toplevel(tmp_path / "work") is None
    assert Git().toplevel(tmp_path / "no-such-place") is None
    assert Git().toplevel(repo / ".git") is None
    assert Git().toplevel(repo / ".git" / "refs") is None
    bare = tmp_path / "bare.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    assert Git().toplevel(bare) is None


def test_worktrees_are_listed_from_either_side(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "main-checkout")
    tree = tmp_path / "feature-tree"
    git(repo, "worktree", "add", "-q", "-b", "feature", str(tree))
    tree = tree.resolve()
    assert Git().toplevel(tree / "src") == tree
    assert set(Git().worktrees(repo)) == {repo, tree}
    assert Git().worktrees(repo)[0] == repo
    assert set(Git().worktrees(tree)) == {repo, tree}
    assert Git().worktrees(tree)[0] == tree
    _same_as_git(tree)


def test_a_worktree_whose_directory_is_gone_is_left_out(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "main-checkout")
    tree = tmp_path / "gone-tree"
    git(repo, "worktree", "add", "-q", "-b", "gone", str(tree))
    for path in sorted(tree.rglob("*"), key=lambda p: -len(p.parts)):
        path.rmdir() if path.is_dir() and not path.is_symlink() else path.unlink()
    tree.rmdir()
    assert Git().worktrees(repo) == (repo,)


def test_a_failed_read_logs_its_operation_and_error_class_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    repo = make_repo(tmp_path / "private-plans")

    def refuse(_repo: pygit2.Repository) -> None:
        raise pygit2.GitError(f"failed to read {repo}/.git/index: token=hunter2")

    monkeypatch.setattr(git_module, "_status", refuse)
    with caplog.at_level(logging.INFO, logger="herdr_pwa.files.git"):
        assert Git().status(repo).branch == ""
    shown = journal_text(caplog)
    assert "git status failed: GitError" in shown
    assert "private-plans" not in shown
    assert "hunter2" not in shown


def _snapshot(dotgit: Path) -> dict[str, tuple[int, int]]:
    return {
        str(p.relative_to(dotgit)): (p.stat().st_mtime_ns, p.stat().st_size)
        for p in dotgit.rglob("*")
        if p.is_file()
    }


def test_reading_never_writes_to_the_repository(tmp_path: Path) -> None:
    repo = _busy(tmp_path / "busy")
    # Racily clean: a file rewritten within the index's own timestamp, which git would refresh.
    stamp = (repo / ".git" / "index").stat().st_mtime_ns
    _write(repo, "README.md", (repo / "README.md").read_text().replace("demo", "dEmo"))
    os.utime(repo / "README.md", ns=(stamp, stamp))
    before = _snapshot(repo / ".git")
    reader = Git()
    reader.toplevel(repo)
    reader.worktrees(repo)
    for change in reader.status(repo).changes:
        reader.diff(repo, change.path)
    reader.files(repo)
    assert _snapshot(repo / ".git") == before
