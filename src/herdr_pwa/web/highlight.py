"""A fenced block's code as runs of text, each tagged with one class from a closed set."""

from functools import lru_cache
from typing import Final

from pygments.lexer import Lexer
from pygments.lexers import find_lexer_class_by_name
from pygments.token import (
    Comment,
    Generic,
    Keyword,
    Literal,
    Name,
    Number,
    Operator,
    String,
    _TokenType,  # pyright: ignore[reportPrivateUsage]
)
from pygments.util import ClassNotFound

from herdr_pwa.web.mdnode import MdNode

# The most specific entry wins; a type with no entry up its chain stays plain text.
_CLASSES: Final[dict[_TokenType, str]] = {
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


def highlight(code: str, language: str) -> tuple[MdNode, ...]:
    """Runs whose text joins back into ``code``, or none for an unknown or absent language."""
    lexer = _lexer(language.lower())
    if lexer is None or len(code) > HIGHLIGHT_LIMIT:
        return ()
    runs: list[tuple[str, str]] = []
    for kind, text in lexer.get_tokens(code):
        cls = _class(kind)
        if runs and runs[-1][0] == cls:
            runs[-1] = (cls, runs[-1][1] + text)
        else:
            runs.append((cls, text))
    return tuple(_run(cls, text) for cls, text in runs if text)
