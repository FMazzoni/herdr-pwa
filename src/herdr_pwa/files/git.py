"""What git knows about a root: where it starts, its worktrees, what changed, and one file's diff."""

import logging
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

log = logging.getLogger(__name__)

TIMEOUT: Final = 5.0
# git status is the expensive read and every page of a root asks for it.
STATUS_TTL: Final = 3.0
FILES_TTL: Final = 5.0
# Where a cwd's repository starts and which worktrees it has barely ever changes.
LAYOUT_TTL: Final = 30.0
# git's well-known empty tree, to diff against in a repository with no commit yet.
EMPTY_TREE: Final = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
# No optional locks (status never blocks an agent's commit), no prompt, no system config.
_ENV: Final = {
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_CONFIG_NOSYSTEM": "1",
    "LC_ALL": "C",
}
# An agent writes to these repositories, and a repository's own config can name a command
# for git to run: an fsmonitor hook on status, an external diff, a pager, hooks. Each is
# switched off on the command line, which outranks every config file.
HARDENED: Final = (
    "-c",
    "core.fsmonitor=",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.pager=cat",
    "-c",
    "diff.external=",
    "-c",
    "core.quotepath=off",
)
# A filter driver's command runs on diff and status for any path its attributes name, and
# .git/info/attributes can name one too; each driver the config defines is blanked by name.
_FILTER_KEYS: Final = ("clean", "smudge", "process")


def _filter_names(raw: bytes) -> list[str] | None:
    """Driver names from ``git config --null --get-regexp ^filter[.]``; None if one is unsafe."""
    names: list[str] = []
    for entry in raw.decode("utf-8", "replace").split("\0"):
        key = entry.split("\n", 1)[0]
        if not key.startswith("filter."):
            continue
        name = key[len("filter.") :].rpartition(".")[0]
        # git splits -c at its first "=", so a name holding one cannot be blanked.
        if "=" in name or not name:
            return None
        names.append(name)
    return list(dict.fromkeys(names))


def _blanked(names: list[str]) -> list[str]:
    args: list[str] = []
    for name in names:
        for key in _FILTER_KEYS:
            args += ["-c", f"filter.{name}.{key}="]
        args += ["-c", f"filter.{name}.required=false"]
    return args


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


def parse_status(raw: bytes) -> Status:
    """``git status --porcelain=v1 -z -b`` output, as a branch and its changes."""
    fields = raw.decode("utf-8", "replace").split("\0")
    branch = ""
    changes: list[Change] = []
    skip = False
    for item in fields:
        if skip:
            # A rename's second field is the old name, which this view does not show.
            skip = False
            continue
        if item.startswith("## "):
            branch = item[3:].split("...", 1)[0]
        elif item[2:3] == " " and item[3:]:
            code = item[:2]
            changes.append(Change(item[3:], code, _kind(code)))
            skip = "R" in code or "C" in code
    return Status(branch, tuple(sorted(changes, key=lambda c: c.path)))


class Git:
    """Runs read-only git commands under a timeout, caching each answer for a few seconds."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.binary = shutil.which("git") or ""
        self._memo: dict[tuple[str, ...], tuple[float, object]] = {}

    def _spawn(self, cwd: Path, args: list[str]) -> bytes | None:
        command = [self.binary, "-C", str(cwd), *HARDENED, *args]
        try:
            # A fixed argument list with no shell; the binary is the one on this box's PATH.
            # No stdin: a command a repository names must not wait on the server's.
            done = subprocess.run(  # noqa: S603
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=TIMEOUT,
                check=False,
                env={**os.environ, **_ENV},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.info("git %s in %s failed: %s", args[0], cwd, exc)
            return None
        return done.stdout if done.returncode == 0 else None

    def run(self, cwd: Path, *args: str) -> bytes | None:
        """Standard output of one git command, or None when it fails, times out or is absent."""
        if not self.binary:
            return None
        # Reading config runs nothing; it is read afresh so a driver added a moment ago is seen.
        found = self._spawn(cwd, ["config", "--null", "--get-regexp", "^filter[.]"])
        names = _filter_names(found or b"")
        if names is None:
            log.warning("git in %s: a filter driver name cannot be blanked; not run", cwd)
            return None
        return self._spawn(cwd, [*_blanked(names), *args])

    def _cached[T](self, key: tuple[str, ...], ttl: float, read: Callable[[], T]) -> T:
        now = self.clock()
        hit = self._memo.get(key)
        if hit is not None and now - hit[0] < ttl:
            return hit[1]  # pyright: ignore[reportReturnType]
        value = read()
        self._memo[key] = (now, value)
        return value

    def toplevel(self, cwd: Path) -> Path | None:
        """The resolved top of the work tree holding ``cwd``, or None outside any repository."""

        def read() -> Path | None:
            out = self.run(cwd, "rev-parse", "--show-toplevel")
            return Path(out.decode().strip()).resolve() if out else None

        return self._cached(("top", str(cwd)), LAYOUT_TTL, read)

    def worktrees(self, top: Path) -> tuple[Path, ...]:
        """Every worktree of the repository at ``top``, ``top`` included."""

        def read() -> tuple[Path, ...]:
            out = self.run(top, "worktree", "list", "--porcelain")
            lines = out.decode("utf-8", "replace").splitlines() if out else []
            found = [Path(line[9:]).resolve() for line in lines if line.startswith("worktree ")]
            return tuple(dict.fromkeys([top, *found]))

        return self._cached(("worktrees", str(top)), LAYOUT_TTL, read)

    def status(self, top: Path) -> Status:
        """Branch and changes, untracked files listed one by one."""

        def read() -> Status:
            out = self.run(top, "status", "--porcelain=v1", "-z", "-b", "--untracked-files=all")
            return parse_status(out) if out is not None else Status("", ())

        return self._cached(("status", str(top)), STATUS_TTL, read)

    def files(self, top: Path) -> frozenset[str]:
        """Every tracked path, and every untracked one that is not ignored."""

        def read() -> frozenset[str]:
            out = self.run(top, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
            names = out.decode("utf-8", "replace").split("\0") if out else []
            return frozenset(n for n in names if n)

        return self._cached(("files", str(top)), FILES_TTL, read)

    def diff(self, top: Path, path: str) -> str | None:
        """The working tree against HEAD for one path, with no external diff or textconv."""
        base = "HEAD" if self.run(top, "rev-parse", "--verify", "-q", "HEAD") else EMPTY_TREE
        args = ("diff", "--no-ext-diff", "--no-textconv", "--no-color", "-M", base, "--", path)
        out = self.run(top, *args)
        return out.decode("utf-8", "replace") if out is not None else None
