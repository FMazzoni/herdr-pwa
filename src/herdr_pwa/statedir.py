"""The state directory at 0700 and the files in it at 0600, each mode checked after the fact."""

import os
import stat
import tempfile
from pathlib import Path
from typing import Final

DIR_MODE: Final = 0o700
FILE_MODE: Final = 0o600


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
