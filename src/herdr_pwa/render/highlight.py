"""A fenced block's code as runs of text, each tagged with one class from a closed set."""

from collections.abc import Sequence
from functools import lru_cache
from types import MappingProxyType
from typing import Final

from pygments.lexer import Lexer
from pygments.lexers import find_lexer_class_by_name, find_lexer_class_for_filename
from pygments.token import (
    Comment,
    Generic,
    Keyword,
    Literal,
    Name,
    Number,
    Operator,
    String,
    # pygments exports no public name for a token's type, and the class map is keyed on it.
    _TokenType,  # pyright: ignore[reportPrivateUsage] - pygments has no public name for a token type
)
from pygments.util import ClassNotFound

from herdr_pwa.render.mdnode import MdNode

# The most specific entry wins; a type with no entry up its chain stays plain text.
_CLASSES: Final[MappingProxyType[_TokenType, str]] = MappingProxyType(
    {
        Keyword: "tok-keyword",
        Keyword.Constant: "tok-constant",
        Keyword.Type: "tok-type",
        Operator.Word: "tok-keyword",
        Name.Builtin: "tok-builtin",
        Name.Builtin.Pseudo: "tok-constant",
        Name.Function: "tok-function",
        Name.Decorator: "tok-function",
        Name.Class: "tok-type",
        Name.Exception: "tok-type",
        Name.Tag: "tok-keyword",
        Name.Attribute: "tok-builtin",
        Name.Variable: "tok-variable",
        String: "tok-string",
        String.Interpol: "tok-variable",
        Literal.Number: "tok-number",
        Number: "tok-number",
        Comment: "tok-comment",
        Generic.Prompt: "tok-comment",
        Generic.Output: "tok-comment",
        Generic.Heading: "tok-keyword",
        Generic.Subheading: "tok-keyword",
        Generic.Inserted: "tok-inserted",
        Generic.Deleted: "tok-deleted",
    }
)
TOKEN_CLASSES: Final = frozenset(_CLASSES.values())
# Past this a block is shown plain: lexing runs on the server for every poll that misses the cache.
HIGHLIGHT_LIMIT: Final = 20_000


@lru_cache(maxsize=64)
def _lexer(name: str) -> Lexer | None:
    if not name:
        return None
    try:
        # Lexers trim and pad by default; the copy button must hand back the code as written.
        return find_lexer_class_by_name(name)(stripnl=False, stripall=False, ensurenl=False)
    except ClassNotFound:
        return None


def _class(kind: _TokenType | None) -> str:
    while kind is not None and kind not in _CLASSES:
        kind = kind.parent
    return _CLASSES.get(kind, "") if kind is not None else ""


def _run(cls: str, text: str) -> MdNode:
    return MdNode("span", cls=cls, text=text) if cls else MdNode(text=text)


def highlight(code: str, language: str, limit: int = HIGHLIGHT_LIMIT) -> tuple[MdNode, ...]:
    """Runs whose text joins back into ``code``, or none for an unknown or absent language."""
    lexer = _lexer(language.lower())
    if lexer is None or len(code) > limit:
        return ()
    runs: list[tuple[str, str]] = []
    for kind, text in lexer.get_tokens(code):
        cls = _class(kind)
        if runs and runs[-1][0] == cls:
            runs[-1] = (cls, runs[-1][1] + text)
        else:
            runs.append((cls, text))
    return tuple(_run(cls, text) for cls, text in runs if text)


@lru_cache(maxsize=256)
def language_for(filename: str) -> str:
    """The name of the lexer Pygments picks for ``filename``, or "" for none."""
    try:
        found = find_lexer_class_for_filename(filename)
    except ClassNotFound:
        return ""
    aliases: Sequence[str] = found.aliases if found is not None else ()
    return aliases[0] if aliases else ""


def split_lines(runs: tuple[MdNode, ...]) -> list[tuple[MdNode, ...]]:
    """Highlighted runs cut at each newline, one tuple of runs per line of the source."""
    lines: list[list[MdNode]] = [[]]
    for run in runs:
        parts = run.text.split("\n")
        for index, part in enumerate(parts):
            if index:
                lines.append([])
            if part:
                lines[-1].append(MdNode(run.tag, cls=run.cls, text=part))
    return [tuple(line) for line in lines]
