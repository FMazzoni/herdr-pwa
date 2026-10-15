"""What git knows about a root: where it starts, its worktrees, what changed, and one file's diff."""

import logging
import os
import selectors
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

log = logging.getLogger(__name__)

TIMEOUT: Final = 5.0
# git's output is read only this far; past it the process is killed. A status or a file list
# of a huge tree fits; one file's diff is cut much sooner, since the phone shows 3,000 rows.
OUTPUT_BYTES: Final = 32 * 1024 * 1024
DIFF_BYTES: Final = 4 * 1024 * 1024
_CHUNK: Final = 64 * 1024
# git status is the expensive read and every page of a root asks for it.
STATUS_TTL: Final = 3.0
FILES_TTL: Final = 5.0
# Where a cwd's repository starts and which worktrees it has barely ever changes.
LAYOUT_TTL: Final = 30.0
# git's well-known empty tree, to diff against in a repository with no commit yet.
EMPTY_TREE: Final = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
# A .git file, or a worktree's gitdir file, is one line holding a path.
GITFILE_BYTES: Final = 4096
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
    # A submodule is a repository with its own config, whose filters the blanking below never
    # reads; git must not step into one, nor summarise one by running git log inside it.
    "-c",
    "submodule.recurse=false",
    "-c",
    "diff.ignoreSubmodules=all",
    "-c",
    "diff.submodule=short",
    "-c",
    "status.submoduleSummary=false",
)
# Said again on status and diff, where it outranks .gitmodules' own submodule.<name>.ignore.
NO_SUBMODULES: Final = "--ignore-submodules=all"
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


def _collect(proc: subprocess.Popen[bytes], limit: int, deadline: float) -> tuple[bytes, bool]:
    """Up to ``limit`` bytes of the process's output, and whether that was all of it."""
    assert proc.stdout is not None  # noqa: S101 - spawned with stdout=PIPE
    out = bytearray()
    with selectors.DefaultSelector() as ready:
        ready.register(proc.stdout, selectors.EVENT_READ)
        while len(out) <= limit:
            left = deadline - time.monotonic()
            if left <= 0 or not ready.select(left):
                raise subprocess.TimeoutExpired(proc.args, TIMEOUT)
            chunk = os.read(proc.stdout.fileno(), _CHUNK)
            if not chunk:
                return bytes(out), True
            out += chunk
    return bytes(out[:limit]), False


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

    def _spawn(self, cwd: Path, args: list[str], limit: int = OUTPUT_BYTES) -> bytes | None:
        command = [self.binary, "-C", str(cwd), *HARDENED, *args]
        try:
            # A fixed argument list with no shell; the binary is the one on this box's PATH.
            # No stdin: a command a repository names must not wait on the server's.
            proc = subprocess.Popen(  # noqa: S603
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env={**os.environ, **_ENV},
            )
        except OSError as exc:
            log.info("git %s in %s failed: %s", args[0], cwd, exc)
            return None
        try:
            out, whole = _collect(proc, limit, time.monotonic() + TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.info("git %s in %s failed: %s", args[0], cwd, exc)
            return None
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            if proc.stdout is not None:
                proc.stdout.close()
        return out if not whole or proc.returncode == 0 else None

    def run(self, cwd: Path, *args: str, limit: int = OUTPUT_BYTES) -> bytes | None:
        """Standard output of one git command, cut at ``limit``; None when it fails or times out."""
        if not self.binary:
            return None
        # Reading config runs nothing; it is read afresh so a driver added a moment ago is seen.
        found = self._spawn(cwd, ["config", "--null", "--get-regexp", "^filter[.]"])
        names = _filter_names(found or b"")
        if names is None:
            log.warning("git in %s: a filter driver name cannot be blanked; not run", cwd)
            return None
        return self._spawn(cwd, [*_blanked(names), *args], limit)

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
            top = Path(out.decode().strip()).resolve() if out else None
            # core.worktree can name any directory; a top that does not hold the cwd is not one.
            here = cwd.resolve()
            return top if top is not None and (here == top or top in here.parents) else None

        return self._cached(("top", str(cwd)), LAYOUT_TTL, read)

    def worktrees(self, top: Path) -> tuple[Path, ...]:
        """Every worktree of the repository at ``top``, ``top`` included."""
        return self._cached(("worktrees", str(top)), LAYOUT_TTL, lambda: self._worktrees(top))

    def _worktrees(self, top: Path) -> tuple[Path, ...]:
        out = self.run(top, "worktree", "list", "--porcelain")
        lines = out.decode("utf-8", "replace").splitlines() if out else []
        found = [Path(line[9:]).resolve() for line in lines if line.startswith("worktree ")]
        layout = self._layout(top)
        if layout is None or layout[0] != top:
            return (top,)
        # The list is read from files in .git that an agent can write, so each entry
        # must also claim this repository from its own side before it is believed.
        kept = [t for t in dict.fromkeys(found) if t == top or self._claims(t, layout[1])]
        return tuple(dict.fromkeys([top, *kept]))

    def _layout(self, tree: Path) -> tuple[Path, Path] | None:
        out = self.run(
            tree, "rev-parse", "--path-format=absolute", "--show-toplevel", "--git-common-dir"
        )
        match out.decode("utf-8", "replace").splitlines() if out else []:
            case [tree_top, common]:
                return Path(tree_top).resolve(), Path(common).resolve()
            case _:
                return None

    def _claims(self, tree: Path, common: Path) -> bool:
        layout = self._layout(tree)
        return layout == (tree, common) and points_back(tree / ".git", common)

    def status(self, top: Path) -> Status:
        """Branch and changes, untracked files listed one by one."""

        def read() -> Status:
            args = ("status", "--porcelain=v1", "-z", "-b", "--untracked-files=all", NO_SUBMODULES)
            out = self.run(top, *args)
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
        args = ("diff", "--no-ext-diff", "--no-textconv", "--no-color", NO_SUBMODULES, "-M", base)
        out = self.run(top, *args, "--", path, limit=DIFF_BYTES)
        if out is None:
            return None
        # A diff cut at the cap ends on its last whole line.
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
