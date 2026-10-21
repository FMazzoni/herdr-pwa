"""A compiled pattern's record: what it must match and must not, under the call its reader makes."""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

type Method = Literal["match", "fullmatch", "search"]


@dataclass(frozen=True, slots=True)
class Rx:
    """One pattern with examples it matches and rejects under *method*, as its reader calls it."""

    pattern: re.Pattern[str]
    matches: tuple[str, ...]
    rejects: tuple[str, ...]
    method: Method = "search"
    # A validator decides whether outside input is accepted, so it is also fuzzed from its grammar.
    validator: bool = False

    def found(self, text: str) -> re.Match[str] | None:
        """The match *method* finds in *text*, or ``None``."""
        if self.method == "fullmatch":
            return self.pattern.fullmatch(text)
        if self.method == "match":
            return self.pattern.match(text)
        return self.pattern.search(text)


def alternatives(words: Iterable[str]) -> str:
    """*words* as one regex alternation, each escaped, for a pattern to compose in a group."""
    return "|".join(re.escape(word) for word in words)
