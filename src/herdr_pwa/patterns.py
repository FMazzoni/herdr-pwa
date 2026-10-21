"""Pieces the app's patterns are composed from."""

import re
from collections.abc import Iterable


def alternatives(words: Iterable[str]) -> str:
    """*words* as one regex alternation, each escaped, for a pattern to compose in a group."""
    return "|".join(re.escape(word) for word in words)
