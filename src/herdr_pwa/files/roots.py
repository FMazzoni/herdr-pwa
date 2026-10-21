"""Which directories may be browsed: the git work trees agents stand in, and their worktrees."""

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from herdr_pwa.files.git import Git
from herdr_pwa.files.reader import NotReadable, OutsideRoot, inside

ROOT_ID: Final = re.compile(r"[0-9a-f]{12}")
# Credentials and this app's own state never show, even inside a root, even behind a tap.
DENIED_UNDER_HOME: Final = (
    ".ssh",
    ".gnupg",
    ".aws",
    ".claude",
    # Every tool's tokens: gh's hosts.yml, keyrings, this app's own state among them.
    ".config",
    ".local/share",
    ".netrc",
    ".git-credentials",
    ".docker",
    ".kube",
)
# Names that may hold a secret, shown only after a tap; DOTALL, since a name may hold a \n.
_SECRET: Final = re.compile(
    r"""(?isx) ^(
        \.env.* | id_.* | credentials.* | secrets\..*
      | \.npmrc | \.pypirc | \.htpasswd | \.netrc | \.git-credentials
      | .*\.(pem | key | p12 | pfx | kdbx)
      # A token by itself or at a name's end, bare or as data: never tokenizer.py or tokens.css.
      | (.*[._-])? tokens? (\.(txt | json | ya?ml | toml | ini | conf | cfg))?
    )$"""
)
# Git's own directory: config holds remote URLs with tokens; the git views read it through pygit2.
_GIT_DIR: Final = ".git"


@dataclass(frozen=True, slots=True)
class Root:
    """One browsable work tree: an opaque id for URLs, its resolved path, and who is in it."""

    id: str
    path: Path
    label: str
    sessions: tuple[str, ...]


def root_id(path: Path) -> str:
    """An id that names ``path`` without spelling it in a URL."""
    return hashlib.sha256(str(path).encode()).hexdigest()[:12]


def is_git_dir(name: str) -> bool:
    """Whether a directory entry is git's own, which the explorer never lists or reads."""
    return name.lower() == _GIT_DIR


def is_secret(name: str) -> bool:
    """Whether a file's name says it may hold a secret."""
    return _SECRET.match(name) is not None


@dataclass(frozen=True, slots=True)
class Fence:
    """What no root may be or hold: home and its ancestors, and the denied directories."""

    home: Path
    denied: tuple[Path, ...]

    @classmethod
    def around(cls, home: Path, *extra: Path) -> "Fence":
        """The fence for this box: home's credential stores, this app's state, and ``extra``."""
        home = home.resolve()
        denied = tuple(home / d for d in DENIED_UNDER_HOME) + tuple(p.resolve() for p in extra)
        return cls(home, denied)

    def may_root(self, path: Path) -> bool:
        """A root is never ``/``, home, a directory above home, or anything denied."""
        return path != Path(path.anchor) and not inside(self.home, path) and self.allows(path)

    def allows(self, path: Path) -> bool:
        """Whether a resolved path is clear of every denied directory."""
        text = str(path)
        # A prefix match, so a state dir's backup sibling or a .netrc-old is fenced as well.
        return not any(inside(path, d) or text.startswith(str(d)) for d in self.denied)


def _owners(cwds: Iterable[tuple[str, str]], git: Git) -> dict[Path, list[str]]:
    owners: dict[Path, list[str]] = {}
    for cwd, session in cwds:
        top = git.toplevel(Path(cwd)) if cwd.startswith("/") else None
        if top is None:
            continue
        for tree in git.worktrees(top):
            owners.setdefault(tree, [])
        owners[top].append(session)
    return owners


def discover(cwds: Iterable[tuple[str, str]], git: Git, fence: Fence) -> tuple[Root, ...]:
    """Every root the herd stands in, from (cwd, session) pairs; a non-repository cwd adds none."""
    roots = [
        Root(root_id(path), path, path.name or str(path), tuple(dict.fromkeys(people)))
        for path, people in _owners(cwds, git).items()
        if fence.may_root(path) and path.is_dir()
    ]
    # Roots someone is working in first, then by name.
    return tuple(sorted(roots, key=lambda r: (not r.sessions, r.label.lower(), str(r.path))))


def resolve(root: Root, rel: str, fence: Fence) -> Path:
    """``rel`` resolved, links and all; raises unless it is inside ``root``, fenced, not in .git."""
    if "\0" in rel:
        raise OutsideRoot("a NUL in a path")
    real = (root.path / rel).resolve()
    if not inside(real, root.path) or not fence.allows(real):
        raise OutsideRoot(rel)
    if any(is_git_dir(part) for part in real.relative_to(root.path).parts):
        raise OutsideRoot(rel)
    _probe(real)
    return real


def _probe(real: Path) -> None:
    # A name past NAME_MAX, or a parent it may not search, raises from every later is_dir().
    try:
        real.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return
    except OSError as exc:
        raise NotReadable(exc.strerror or "unreadable") from exc


def relative(root: Root, real: Path) -> str:
    """A resolved path inside ``root`` as the root-relative name URLs carry; "" for the root."""
    return "" if real == root.path else real.relative_to(root.path).as_posix()


class UnknownRoot(Exception):  # noqa: N818 - named for what the screen says
    """A root id no agent is standing in now, or an agent standing in no repository."""
