"""Bytes from disk behind one small interface: list a directory, stat a file, read a range."""

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

# A directory past this many entries is shown cut, with the count of what was left out.
MAX_ENTRIES: Final = 2000
_FILE_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_DIR_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class OutsideRoot(Exception):  # noqa: N818 - named for what the screen says
    """A path that resolves outside every root the herd is working in."""


class NotReadable(Exception):  # noqa: N818 - named for what the screen says
    """A path that is missing, or is not a regular file or directory."""


@dataclass(frozen=True, slots=True)
class Entry:
    """One name in a directory, as the directory itself sees it (links are not followed)."""

    name: str
    is_dir: bool
    size: int


@dataclass(frozen=True, slots=True)
class Stat:
    """What a file's view is keyed on: a file that changes changes one of these."""

    size: int
    mtime_ns: int
    is_dir: bool

    @property
    def version(self) -> str:
        """A short token that moves whenever the file does."""
        return f"{self.mtime_ns:x}-{self.size:x}"


@dataclass(frozen=True, slots=True)
class Listing:
    """A directory's entries, and how many more there were than are shown."""

    entries: tuple[Entry, ...]
    more: int


class Reader(Protocol):
    """Where a root's bytes come from; a remote reader would implement the same three calls."""

    def list_dir(self, path: Path) -> Listing:
        """The entries of ``path``, which must be a directory inside the root."""
        ...

    def stat(self, path: Path) -> Stat:
        """Size, modification time and kind of ``path``."""
        ...

    def read_range(self, path: Path, offset: int, length: int) -> bytes:
        """Up to ``length`` bytes of the regular file ``path`` from ``offset``."""
        ...


def inside(path: Path, root: Path) -> bool:
    """Whether ``path`` is ``root`` or lies under it; both must already be resolved."""
    return path == root or root in path.parents


class LocalReader:
    """Reads this box's disk, checking each opened descriptor still lies inside ``root``."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _confined(self, fd: int) -> None:
        # What was opened, so a swapped-in symlink is caught; no /proc is no proof, so refuse.
        try:
            opened = Path(f"/proc/self/fd/{fd}").readlink()
        except OSError as exc:
            raise OutsideRoot(str(exc)) from exc
        if not inside(opened, self.root):
            raise OutsideRoot(str(opened))

    def _open(self, path: Path, flags: int) -> int:
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            raise NotReadable(str(exc)) from exc
        try:
            self._confined(fd)
        except OutsideRoot:
            os.close(fd)
            raise
        return fd

    def list_dir(self, path: Path) -> Listing:
        """Entries of a directory, sorted directories first, cut at ``MAX_ENTRIES``."""
        fd = self._open(path, _DIR_FLAGS)
        try:
            found = [_entry(e) for e in os.scandir(fd)]
        finally:
            os.close(fd)
        found.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return Listing(tuple(found[:MAX_ENTRIES]), max(0, len(found) - MAX_ENTRIES))

    def stat(self, path: Path) -> Stat:
        """The kind, size and time of ``path``; a FIFO, socket or device is not readable."""
        try:
            found = path.stat()
        except OSError as exc:
            raise NotReadable(str(exc)) from exc
        is_dir = stat.S_ISDIR(found.st_mode)
        if not is_dir and not stat.S_ISREG(found.st_mode):
            raise NotReadable(f"{path.name} is not a regular file")
        return Stat(found.st_size, found.st_mtime_ns, is_dir)

    def read_range(self, path: Path, offset: int, length: int) -> bytes:
        """Bytes of a regular file; opened non-blocking, so a FIFO can never hang a request."""
        fd = self._open(path, _FILE_FLAGS)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise NotReadable(f"{path.name} is not a regular file")
            return os.pread(fd, length, offset)
        finally:
            os.close(fd)


def _entry(found: os.DirEntry[str]) -> Entry:
    try:
        is_dir = found.is_dir(follow_symlinks=False)
        size = found.stat(follow_symlinks=False).st_size
    except OSError:
        is_dir, size = False, 0
    return Entry(found.name, is_dir, size)
