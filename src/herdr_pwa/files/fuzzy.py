"""Go to file: the paths a short typed query most likely means, best first, capped."""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

MAX_RESULTS: Final = 50
MAX_QUERY: Final = 120
_SEPARATORS: Final = frozenset("/_-. ")


@dataclass(frozen=True, slots=True)
class Found:
    """The best matches and how many paths matched in all."""

    paths: tuple[str, ...]
    total: int


def _bonus(path: str, at: int, previous: int) -> int:
    score = 1
    # A run of typed letters found together beats the same letters scattered over folders.
    if at == previous + 1:
        score += 8
    if at == 0 or path[at - 1] in _SEPARATORS:
        score += 4
    if "/" not in path[at:]:
        # The file's own name counts for more than the folders above it.
        score += 2
    return score


def score(query: str, path: str) -> int | None:
    """How well ``path`` matches ``query`` as a subsequence, or None if it does not."""
    lower = path.lower()
    total = 0
    previous = -2
    at = -1
    for char in query:
        at = lower.find(char, at + 1)
        if at == -1:
            return None
        total += _bonus(lower, at, previous)
        previous = at
    return total


def find(query: str, paths: Iterable[str]) -> Found:
    """The ``MAX_RESULTS`` best paths for ``query``; spaces are ignored, case does not matter."""
    wanted = "".join(query.lower().split())[:MAX_QUERY]
    if not wanted:
        return Found((), 0)
    scored = [(s, p) for p in paths if (s := score(wanted, p)) is not None]
    scored.sort(key=lambda sp: (-sp[0], len(sp[1]), sp[1]))
    return Found(tuple(p for _, p in scored[:MAX_RESULTS]), len(scored))
