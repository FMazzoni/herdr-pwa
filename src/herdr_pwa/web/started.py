"""The source tree the service started from, recorded so ``doctor`` can say it has moved since."""

import errno
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, ValidationError

from herdr_pwa import statedir

log = logging.getLogger(__name__)

PACKAGE_DIR: Final = Path(__file__).parent.parent
STARTED_FILENAME: Final = "service-tree"
_DIGEST_CHARS: Final = 12


class StartedTree(BaseModel):
    """Where the service's code was, what it hashed to, and when the service started."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    directory: str
    fingerprint: str
    at: float


@dataclass(frozen=True, slots=True)
class TreeCheck:
    """The line ``doctor`` prints about the tree, and whether it is a failure."""

    line: str
    stale: bool = field(kw_only=True)


def fingerprint(directory: Path) -> str:
    """Hash every file's name and content under ``directory``, never mtimes; bytecode is skipped.

    Raises:
        OSError: the directory or a file in it cannot be read.
    """
    if not directory.is_dir():
        raise FileNotFoundError(errno.ENOENT, "no such directory", str(directory))
    whole = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        name = path.relative_to(directory)
        if "__pycache__" in name.parts or not path.is_file():
            continue
        whole.update(name.as_posix().encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return whole.hexdigest()[:_DIGEST_CHARS]


def record_started(state_dir: Path, at: float) -> None:
    """Write the tree this process runs from where ``doctor`` reads it; failures are logged."""
    try:
        tree = fingerprint(PACKAGE_DIR)
        started = StartedTree(directory=str(PACKAGE_DIR), fingerprint=tree, at=at)
        _ = statedir.ensure_dir(state_dir)
        statedir.write_secret(state_dir / STARTED_FILENAME, started.model_dump_json() + "\n")
    except OSError as exc:
        log.warning("could not record the service's tree for doctor: %s", type(exc).__name__)


def started_tree(state_dir: Path) -> StartedTree | None:
    """The tree the service last started from, or ``None`` when it never said."""
    try:
        text = statedir.read_text(state_dir / STARTED_FILENAME)
        return None if text is None else StartedTree.model_validate_json(text)
    except (OSError, ValidationError):
        return None


def check_tree(state_dir: Path) -> TreeCheck:
    """Compare the tree the service started from with what that directory holds now."""
    started = started_tree(state_dir)
    if started is None:
        return TreeCheck(
            "service tree: never recorded, so a stale service is not ruled out", stale=False
        )
    when = datetime.fromtimestamp(started.at, UTC).strftime("%Y-%m-%d %H:%M UTC")
    head = f"service tree at {started.directory}, started {when} from {started.fingerprint}"
    try:
        now = fingerprint(Path(started.directory))
    except OSError as exc:
        return TreeCheck(f"{head}: unreadable now ({exc.strerror})  FAIL", stale=True)
    if now != started.fingerprint:
        return TreeCheck(f"{head}: now {now}, so it serves old code; restart it  FAIL", stale=True)
    return TreeCheck(f"{head}: unchanged  ok", stale=False)
