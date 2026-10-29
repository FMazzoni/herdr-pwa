"""What git knows about a root: where it starts, its worktrees, what changed, and one file's diff."""

import logging
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final, cast

import pygit2
from pygit2.enums import DeltaStatus, DiffFind, DiffOption, RepositoryOpenFlag

from herdr_pwa.clock import MonoClock, mono_now

log = logging.getLogger(__name__)

# One file's diff is cut here, since the phone shows 3,000 rows.
DIFF_BYTES: Final = 4 * 1024 * 1024
# A status is the expensive read and every page of a root asks for it.
STATUS_TTL: Final = 3.0
FILES_TTL: Final = 5.0
# Where a cwd's repository starts and which worktrees it has barely ever changes.
LAYOUT_TTL: Final = 30.0
# git's well-known empty tree, to diff against in a repository with no commit yet.
EMPTY_TREE: Final = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
# A .git file, a worktree's gitdir file or a commondir file is one line holding a path.
GITFILE_BYTES: Final = 4096
# libgit2 runs no repo-named filter, fsmonitor, hook or textconv; submodules shut, no locks taken.
_READ: Final = DiffOption.IGNORE_SUBMODULES
_STATUS: Final = _READ | DiffOption.INCLUDE_TYPECHANGE
_UNTRACKED: Final = _STATUS | DiffOption.INCLUDE_UNTRACKED | DiffOption.RECURSE_UNTRACKED_DIRS
# What a read of a repository an agent is rewriting under us can raise; each is a failed read.
_FAILED: Final = (pygit2.GitError, OSError, KeyError, ValueError)
# git status's two-letter code for a conflict, by which of base, ours and theirs exist.
_CONFLICT: Final = MappingProxyType(
    {
        (True, False, False): "DD",
        (False, True, False): "AU",
        (True, True, False): "UD",
        (False, False, True): "UA",
        (True, False, True): "DU",
        (False, True, True): "AA",
        (True, True, True): "UU",
    }
)


@dataclass(frozen=True, slots=True)
class Change:
    """One path git status reports, with its two-letter code and a one-word kind."""

    path: str
    code: str
    kind: str


@dataclass(frozen=True, slots=True)
class Status:
    """A root's branch and every changed path, as of one read."""

    branch: str
    changes: tuple[Change, ...]

    def of(self, path: str) -> Change | None:
        """The change at ``path``, if there is one."""
        return next((c for c in self.changes if c.path == path), None)

    def under(self, folder: str) -> int:
        """How many changes sit inside ``folder`` (a root-relative path, "" for the root)."""
        prefix = f"{folder}/" if folder else ""
        return sum(1 for c in self.changes if c.path.startswith(prefix))


def _kind(code: str) -> str:
    if code == "??":
        return "new"
    for letter, kind in (("U", "conflict"), ("D", "deleted"), ("R", "renamed"), ("A", "added")):
        if letter in code:
            return kind
    return "modified"


def _common(gitdir: Path) -> Path:
    """The repository a git directory belongs to: its commondir file's, else its own."""
    shared = _small(gitdir / "commondir")
    return (gitdir / shared).resolve() if shared else gitdir


def _branch(repo: pygit2.Repository) -> str:
    """The branch as git status -b names it, the upstream left off."""
    if repo.head_is_detached:
        return "HEAD (no branch)"
    target = repo.lookup_reference("HEAD").target
    name = target.removeprefix("refs/heads/") if isinstance(target, str) else ""
    return f"No commits yet on {name}" if repo.head_is_unborn else name


def _head_tree(repo: pygit2.Repository) -> pygit2.Tree:
    # libgit2 knows the empty tree without it being stored, as git does.
    head = repo[EMPTY_TREE] if repo.head_is_unborn else repo.head
    return head.peel(pygit2.Tree)


def _conflicts(index: pygit2.Index) -> dict[str, str]:
    codes: dict[str, str] = {}
    for sides in index.conflicts or ():
        base, ours, theirs = sides
        path = next(os.fspath(s.path) for s in sides if s is not None)
        codes[path] = _CONFLICT[(base is not None, ours is not None, theirs is not None)]
    return codes


def _deltas(diff: pygit2.Diff) -> Iterable[pygit2.DiffDelta]:
    return (d for d in diff.deltas if d.status != DeltaStatus.CONFLICTED)


def _status(repo: pygit2.Repository) -> Status:
    """HEAD against the index, renames found as git status finds them, then the work tree."""
    index = repo.index
    staged = _head_tree(repo).diff_to_index(index, _STATUS)
    staged.find_similar(DiffFind.FIND_RENAMES)
    codes = {d.new_file.path: d.status_char() + " " for d in _deltas(staged)}
    for delta in _deltas(index.diff_to_workdir(_UNTRACKED)):
        path = delta.new_file.path
        untracked = delta.status == DeltaStatus.UNTRACKED
        codes[path] = "??" if untracked else codes.get(path, " ")[0] + delta.status_char()
    codes.update(_conflicts(index))
    changes = (Change(path, code, _kind(code)) for path, code in codes.items())
    return Status(_branch(repo), tuple(sorted(changes, key=lambda c: c.path)))


def _files(repo: pygit2.Repository) -> frozenset[str]:
    """Every index entry, and every untracked path that is not ignored, as git ls-files lists."""
    index = repo.index
    tracked = (os.fspath(index[n].path) for n in range(len(index)))
    loose = index.diff_to_workdir(_UNTRACKED).deltas
    untracked = (d.new_file.path for d in loose if d.status == DeltaStatus.UNTRACKED)
    return frozenset([*tracked, *untracked])


def _patches(
    diff: pygit2.Diff, path: str, keep: Callable[[pygit2.DiffDelta], bool]
) -> list[tuple[str, bytes]]:
    """Each kept delta's patch at ``path`` or under it, as git's pathspec matches."""
    prefix = f"{path}/"
    found: list[tuple[str, bytes]] = []
    for n, delta in enumerate(diff.deltas):
        name = delta.new_file.path
        if (name == path or name.startswith(prefix)) and keep(delta):
            patch = diff[n]
            found.append((name, patch.data if patch is not None else b""))
    return found


def _diff(repo: pygit2.Repository, path: str) -> bytes:
    """git diff HEAD -- path: the tree against the index's view of the work tree."""
    index = repo.index
    tree = _head_tree(repo)
    found = tree.diff_to_index(index, _READ)
    found.merge(index.diff_to_workdir(_READ))
    unmerged = {d.new_file.path for d in found.deltas if d.status == DeltaStatus.CONFLICTED}
    patches = _patches(found, path, lambda d: d.new_file.path not in unmerged)
    if unmerged:
        # git compares an unmerged path's work-tree file with HEAD directly, past the index.
        direct = tree.diff_to_workdir(_READ)
        patches += _patches(direct, path, lambda d: d.new_file.path in unmerged)
    return b"".join(data for _, data in sorted(patches))


def _listed(repo: pygit2.Repository) -> tuple[Path, ...]:
    """The main work tree and each linked one the repository's worktrees directory names."""
    main = [] if repo.is_bare else [Path(repo.workdir).resolve()]
    linked: list[Path] = []
    for name in repo.list_worktrees():
        try:
            linked.append(Path(repo.lookup_worktree(name).path).resolve())
        except _FAILED as exc:
            # The class alone: the error's text and the repository's path name the operator's files.
            log.info("a linked worktree was unreadable: %s", type(exc).__name__)
    return (*main, *linked)


class Git:
    """Reads a repository in-process, caching each answer for a few seconds."""

    def __init__(self, clock: MonoClock = mono_now) -> None:
        self._clock = clock
        self._memo: dict[tuple[str, ...], tuple[float, object]] = {}

    def _read[T](self, what: str, cwd: Path, read: Callable[[pygit2.Repository], T]) -> T | None:
        """``read`` on the repository holding ``cwd``; None outside one or when it fails."""
        try:
            # Searched upward as git does, then opened with no second search.
            found = pygit2.discover_repository(str(cwd))
            if found is None:
                return None
            return read(pygit2.Repository(found, RepositoryOpenFlag.NO_SEARCH))
        except _FAILED as exc:
            # The operation and class alone: the error's text and cwd name the operator's files.
            log.info("git %s failed: %s", what, type(exc).__name__)
            return None

    def _cached[T](self, key: tuple[str, ...], ttl: float, read: Callable[[], T]) -> T:
        now = self._clock()
        hit = self._memo.get(key)
        if hit is not None and now - hit[0] < ttl:
            # One memo holds every read; a key is only ever filled by the same read, so its T.
            return cast("T", hit[1])
        value = read()
        self._memo[key] = (now, value)
        return value

    def toplevel(self, cwd: Path) -> Path | None:
        """The resolved top of the work tree holding ``cwd``, or None outside any repository."""
        return self._cached(("top", str(cwd)), LAYOUT_TTL, lambda: self._layout(cwd)[0])

    def _layout(self, cwd: Path) -> tuple[Path | None, Path | None]:
        """The top of the work tree holding ``cwd``, and its repository's common directory."""

        def read(repo: pygit2.Repository) -> tuple[Path | None, Path | None]:
            gitdir = Path(repo.path).resolve()
            common = _common(gitdir)
            here = cwd.resolve()
            # git answers no top from inside a git directory, nor for a bare repository.
            inside_git = any(d == here or d in here.parents for d in (gitdir, common))
            if repo.is_bare or inside_git:
                return None, None
            top = Path(repo.workdir).resolve()
            # core.worktree can name any directory; a top that does not hold the cwd is not one.
            return (top, common) if here == top or top in here.parents else (None, None)

        return self._read("layout", cwd, read) or (None, None)

    def worktrees(self, top: Path) -> tuple[Path, ...]:
        """Every worktree of the repository at ``top``, ``top`` included."""
        return self._cached(("worktrees", str(top)), LAYOUT_TTL, lambda: self._worktrees(top))

    def _worktrees(self, top: Path) -> tuple[Path, ...]:
        tree_top, common = self._layout(top)
        if tree_top != top or common is None:
            return (top,)
        found = self._read("worktree list", common, _listed) or ()
        # An agent can write the list in .git, so each entry must claim this repository back.
        kept = [t for t in dict.fromkeys(found) if t == top or self._claims(t, common)]
        return tuple(dict.fromkeys([top, *kept]))

    def _claims(self, tree: Path, common: Path) -> bool:
        return self._layout(tree) == (tree, common) and points_back(tree / ".git", common)

    def status(self, top: Path) -> Status:
        """Branch and changes, untracked files listed one by one."""

        def read() -> Status:
            return self._read("status", top, _status) or Status("", ())

        return self._cached(("status", str(top)), STATUS_TTL, read)

    def files(self, top: Path) -> frozenset[str]:
        """Every tracked path, and every untracked one that is not ignored."""

        def read() -> frozenset[str]:
            return self._read("ls-files", top, _files) or frozenset()

        return self._cached(("files", str(top)), FILES_TTL, read)

    def diff(self, top: Path, path: str) -> str | None:
        """The working tree against HEAD for one path, with no external diff or textconv."""
        out = self._read("diff", top, lambda repo: _diff(repo, path))
        if out is None:
            return None
        # A diff cut at the cap ends on its last whole line.
        out = out[:DIFF_BYTES]
        return out[: out.rfind(b"\n") + 1 or len(out)].decode("utf-8", "replace")


def _small(path: Path) -> str | None:
    """The start of a small regular file; never opens a FIFO or a device."""
    try:
        if not path.is_file():
            return None
        with path.open("rb") as handle:
            return handle.read(GITFILE_BYTES).decode("utf-8", "replace").strip()
    except OSError:
        return None


def points_back(dotgit: Path, common: Path) -> bool:
    """Whether ``dotgit`` is this repository's own: its directory, or a worktree's linked file."""
    if dotgit.is_dir() and not dotgit.is_symlink():
        return dotgit.resolve() == common
    text = _small(dotgit) or ""
    if not text.startswith("gitdir: "):
        return False
    admin = (dotgit.parent / text.removeprefix("gitdir: ")).resolve()
    if admin == common:
        # A main work tree whose repository lives elsewhere (git init --separate-git-dir).
        return True
    # A linked worktree: its admin directory is this repository's, and names this file back.
    back = _small(admin / "gitdir")
    return (
        admin.parent == common / "worktrees"
        and back is not None
        and (admin / back).resolve() == dotgit.resolve()
    )
