"""The state directory at 0700 and the files in it at 0600, each mode checked after the fact."""

import fcntl
import os
import stat
import tempfile
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

DIR_MODE: Final = 0o700
FILE_MODE: Final = 0o600
# A holder keeps it for one small write; this long means it is stuck, as a stalled send's 10 s.
LOCK_TIMEOUT: Final = 10.0
# Short, because a holder lets go within milliseconds and LOCK_NB grants no place in a queue.
LOCK_POLL: Final = 0.001


def ensure_dir(path: Path) -> Path:
    """Create the directory 0700 and verify the mode it actually has.

    Raises:
        OSError: the directory cannot be made, or is not 0700 (as ``PermissionError``).
    """
    target = path.expanduser()
    target.mkdir(parents=True, exist_ok=True)
    target.chmod(DIR_MODE)
    mode = stat.S_IMODE(target.stat().st_mode)
    if mode != DIR_MODE:
        raise PermissionError(f"state directory {target} is mode {mode:04o}, expected 0700")
    return target


def write_secret(path: Path, text: str, *, exclusive: bool = False) -> None:
    """Write text at 0600; atomically by rename, or with O_EXCL when exclusive.

    Raises:
        FileExistsError: exclusive and the file already exists.
        OSError: the write failed, or the file ended up other than 0600 (``PermissionError``).
    """
    if exclusive:
        target = path
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
    else:
        # mkstemp opens a fresh 0600 name with O_EXCL, so a planted scratch is never reused.
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        target = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            _ = handle.write(text)
            handle.flush()
            # rename(2) orders nothing on disk; without the fsync a crash can leave the name empty.
            os.fsync(handle.fileno())
        target.chmod(FILE_MODE)
        _check_mode(target)
        if not exclusive:
            _ = target.replace(path)
    except BaseException:
        if not exclusive:
            target.unlink(missing_ok=True)
        raise


def append_line(path: Path, line: str) -> None:
    """Append one line to a 0600 file, creating it if missing.

    Raises:
        OSError: the append failed, or the file is not 0600 (``PermissionError``).
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, FILE_MODE)
    try:
        mode = stat.S_IMODE(os.fstat(fd).st_mode)
        if mode != FILE_MODE:
            raise PermissionError(f"{path} is mode {mode:04o}, expected 0600")
        _ = os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


@contextmanager
def locked(path: Path) -> Generator[None]:
    """Hold an exclusive flock on a 0600 lock file, so other processes' writers wait.

    Raises:
        OSError: the lock file cannot be opened or made 0600, or another holder kept it
            past ``LOCK_TIMEOUT`` (as ``TimeoutError``).
    """
    fd = os.open(path, os.O_RDWR | os.O_CREAT, FILE_MODE)
    try:
        # It holds no secret, so a drifted mode is put back rather than refusing every write.
        if stat.S_IMODE(os.fstat(fd).st_mode) != FILE_MODE:
            os.fchmod(fd, FILE_MODE)
        mode = stat.S_IMODE(os.fstat(fd).st_mode)
        if mode != FILE_MODE:
            raise PermissionError(f"{path} is mode {mode:04o}, expected 0600")
        _acquire(fd, path)
        # Released when the descriptor closes, so a crashed holder never leaves it held.
        yield
    finally:
        os.close(fd)


def _acquire(fd: int, path: Path) -> None:
    # A holder stopped mid-write (Ctrl-Z on a ping) must not hang the service's writers too.
    deadline = time.monotonic() + LOCK_TIMEOUT
    while time.monotonic() < deadline:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            time.sleep(LOCK_POLL)
        else:
            return
    raise TimeoutError(f"{path} stayed locked for {LOCK_TIMEOUT:g}s")


def _check_mode(path: Path) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode != FILE_MODE:
        raise PermissionError(f"{path} is mode {mode:04o}, expected 0600")


def read_text(path: Path) -> str | None:
    """File contents, or None when it does not exist; any other ``OSError`` is raised."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
