"""A harness's SQLite store, read-only and about one session: what hermes and opencode share."""

import sqlite3
from collections.abc import Callable
from contextlib import closing, suppress
from pathlib import Path
from typing import Final, Protocol, Self, cast
from urllib.parse import quote

type Row = tuple[object, ...]

# Long enough for a writer's commit to land, short enough that a held store reads as held.
BUSY_SECONDS: Final = 0.25


class Closable(Protocol):
    """Anything a store read releases when it is done."""

    def close(self) -> None:
        """Release what the store holds open."""
        ...


def readonly_uri(path: Path) -> str:
    """*path* as the only URI this app ever hands SQLite: read-only, never created."""
    return f"file:{quote(str(path))}?mode=ro"


def _locked(exc: sqlite3.Error) -> bool:
    """Is this failure the writer holding the store, not a broken one?"""
    text = str(exc).lower()
    return "locked" in text or "busy" in text


class SessionStore:
    """One session's rows in a store opened read-only, so this reader never takes the lock."""

    def __init__(self, connection: sqlite3.Connection, session: str) -> None:
        self._db = connection
        self.session = session

    @classmethod
    def open(cls, path: Path, *, table: str, session: str) -> Self | None:
        """*path* if it is a store whose *table* holds *session*, else ``None``."""
        try:
            resolved = path.expanduser().resolve(strict=True)
            connection = sqlite3.connect(readonly_uri(resolved), uri=True, timeout=BUSY_SECONDS)
        except (OSError, ValueError, sqlite3.Error):
            return None
        store = cls(connection, session)
        # The table name is the adapter's own constant, never input.
        if store.first(f"select 1 from {table} where id = ? limit 1", session) is None:  # noqa: S608
            store.close()
            return None
        return store

    def rows(self, sql: str, *params: object) -> list[Row]:
        """Every row *sql* answers, or none when the store cannot answer it."""
        try:
            with closing(self._db.execute(sql, params)) as cursor:
                return cast("list[Row]", cursor.fetchall())
        except sqlite3.Error as exc:
            # An older schema reads as no rows; a held store must reach `consult` as held.
            if _locked(exc):
                raise
            return []
        except (OSError, ValueError):
            return []

    def first(self, sql: str, *params: object) -> Row | None:
        """The first row *sql* answers, or ``None``."""
        found = self.rows(sql, *params)
        return found[0] if found else None

    def close(self) -> None:
        """Give the connection back before the WAL grows around it."""
        with suppress(sqlite3.Error):
            self._db.close()


def consult[S: Closable, T](
    opened: Callable[[], S | None], job: Callable[[S], T], *, absent: T, locked: T
) -> T:
    """*job* over the store *opened* finds; *absent* without one, *locked* while it is held."""
    try:
        store = opened()
        if store is None:
            return absent
        with closing(store):
            return job(store)
    except sqlite3.Error as exc:
        # Only a held store gets this far; anything else a query met already read as no rows.
        return locked if _locked(exc) else absent
