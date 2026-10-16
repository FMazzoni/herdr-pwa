"""The state directory at 0700 and the files in it at 0600, each mode checked after the fact."""

import os
import stat
from pathlib import Path

DIR_MODE = 0o700
FILE_MODE = 0o600


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
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    target = path if exclusive else path.with_name(path.name + ".tmp")
    fd = os.open(target, flags, FILE_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            _ = handle.write(text)
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
