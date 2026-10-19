"""Model prose as a tree of plain nodes from a closed tag set, for Jinja to escape."""

import re
from collections.abc import Callable, Iterable
from dataclasses import replace
from functools import lru_cache
from typing import Final
from urllib.parse import urlencode

from linkify_it import LinkifyIt
from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode
from mdit_py_plugins.dollarmath import dollarmath_plugin
from mdit_py_plugins.tasklists import tasklists_plugin
from mdit_py_plugins.texmath import texmath_plugin

from herdr_pwa.render.highlight import TOKEN_CLASSES, highlight, language_for
from herdr_pwa.render.mathml import MATH_TAGS, math_node
from herdr_pwa.render.mdnode import MdNode

__all__ = ["CLASSES", "TAGS", "VOID_TAGS", "MdNode", "render_tree"]


def _parser() -> MarkdownIt:
    # Raw HTML off, so a tag in model text is parsed as text and escaped like any other.
    md = MarkdownIt("commonmark", {"html": False, "linkify": True})
    md.enable(["strikethrough", "table", "linkify"])
    # Only a URL with its scheme: fuzzy matching makes README.md a link to Moldova.
    linkify = LinkifyIt()
    linkify.set({"fuzzy_link": False, "fuzzy_email": False, "fuzzy_ip": False})  # pyright: ignore[reportUnknownMemberType]
    md.linkify = linkify
    # No space inside the dollars and no digit after the closing one, so "$5 and $10" is money.
    md.use(dollarmath_plugin, allow_space=False, allow_digits=False, double_inline=True)
    md.use(texmath_plugin, delimiters="brackets")
    md.use(tasklists_plugin)
    return md


_PARSER: Final = _parser()

TAGS: Final = frozenset(
    {
        *(f"h{n}" for n in range(1, 7)),
        *("p", "ul", "ol", "li", "blockquote", "em", "strong", "s", "a", "code", "pre"),
        *("hr", "br", "table", "thead", "tbody", "tr", "th", "td", "div", "span"),
        *MATH_TAGS,
    }
)
VOID_TAGS: Final = frozenset({"hr", "br"})
CLASSES: Final = frozenset(
    {
        *("table-scroll", "align-center", "align-right", "cell-whole", "cell-token", "math-block"),
        *("task", "task-open", "task-done", "file-ref"),
        *TOKEN_CLASSES,
    }
)
_MATH_MARKS: Final = frozenset("\\^_{}=+<>")
# A lone letter or number, as in "$x$" or "$n$".
_LONE_TERM: Final = re.compile(r"[A-Za-z]|\d+(?:\.\d+)?")
_LINK_SCHEMES: Final = ("http://", "https://")
_LEAF_TEXT: Final = frozenset({"text", "html_block"})
WIDE_TABLE: Final = 4
SHORT_CELL: Final = 24
# A run with no space in it that a browser would still break: after a slash, a backslash or a dash
# (hyphen, en or em), which a sha range, a path or a ticket ref is full of.
_BREAKS: Final = r"/\\\-\u2010\u2013\u2014"
_BREAKABLE_RUN: Final = re.compile(rf"[^\s{_BREAKS}]*[{_BREAKS}]\S*")
_ALIGN: Final = {"text-align:center": "align-center", "text-align:right": "align-right"}
# The tasklists plugin's checkbox: with raw HTML off, no other inline HTML reaches the tree.
_TASK_BOX: Final = "task-list-item-checkbox"
# path:line, as agents and compilers write it: a file with an extension, then a line number,
# then perhaps a column. Not inside a word or a URL, and not followed by more path.
FILE_REF: Final = re.compile(
    r"(?<![\w/.~:@-])((?:\.{1,2}/|/)?(?:[\w.@+-]+/)*[\w@+-][\w.@+-]*\.[A-Za-z][\w]{0,9})"
    r":([1-9]\d{0,6})(?::\d{1,5})?(?![\w/:])"
)


def _names_a_file(path: str) -> bool:
    # A folder in it, or a name Pygments knows, so "example.com:443" and "v1.2:3" stay text.
    return "/" in path or bool(language_for(path.rpartition("/")[2]))


def _file_ref(path: str, line: str, children: tuple[MdNode, ...]) -> MdNode:
    # A query, not a URL: the template puts it under the files route of the agent who wrote it.
    return MdNode(
        "a", cls="file-ref", href=urlencode({"path": path, "line": line}), children=children
    )


def _file_refs(text: str) -> list[MdNode]:
    parts: list[MdNode] = []
    at = 0
    for found in FILE_REF.finditer(text):
        if not _names_a_file(found.group(1)):
            continue
        if found.start() > at:
            parts.append(MdNode(text=text[at : found.start()]))
        parts.append(_file_ref(found.group(1), found.group(2), (MdNode(text=found.group(0)),)))
        at = found.end()
    if at < len(text):
        parts.append(MdNode(text=text[at:]))
    return parts


def _inline_code(node: SyntaxTreeNode) -> MdNode:
    code = MdNode("code", text=node.content)
    found = FILE_REF.fullmatch(node.content.strip())
    if found is None or not _names_a_file(found.group(1)) or _in_link(node):
        return code
    return _file_ref(found.group(1), found.group(2), (code,))


def _in_link(node: SyntaxTreeNode) -> bool:
    parent = node.parent
    while parent is not None:
        if parent.type == "link":
            return True
        parent = parent.parent
    return False


def _code_block(node: SyntaxTreeNode) -> MdNode:
    language = node.info.split(maxsplit=1)[0] if node.info.strip() else ""
    runs = highlight(node.content, language)
    return MdNode("pre", text=node.content, children=runs, label=language[:24])


def _task_box(node: SyntaxTreeNode) -> MdNode:
    if _TASK_BOX not in node.content:
        return MdNode(text=node.content)
    return MdNode("span", cls="task-done" if "checked" in node.content else "task-open")


def _looks_like_math(tex: str) -> bool:
    return any(c in _MATH_MARKS for c in tex) or _LONE_TERM.fullmatch(tex.strip()) is not None


def _inline_math(node: SyntaxTreeNode) -> MdNode:
    # Coding agents write $HOME/$USER in prose; one dollar pair is math only if it reads as math.
    if node.markup == "$" and not _looks_like_math(node.content):
        return MdNode(text=f"${node.content}$")
    return math_node(node.content, block=False)


def _block_math(node: SyntaxTreeNode) -> MdNode:
    return math_node(node.content, block=True)


_LEAVES: Final[dict[str, Callable[[SyntaxTreeNode], MdNode]]] = {
    "code_inline": _inline_code,
    "fence": _code_block,
    "code_block": _code_block,
    "html_inline": _task_box,
    "math_inline": _inline_math,
    "math_inline_double": _inline_math,
    "math_block": _block_math,
    "math_block_eqno": _block_math,
}


def _text_leaf(node: SyntaxTreeNode) -> list[MdNode]:
    if node.type != "text" or _in_link(node) or ":" not in node.content:
        return [MdNode(text=node.content)]
    return _file_refs(node.content) or [MdNode(text=node.content)]


def _leaf(node: SyntaxTreeNode) -> MdNode | None:
    if node.type in _LEAF_TEXT:
        return MdNode(text=node.content)
    if node.type == "softbreak":
        return MdNode(text="\n")
    convert = _LEAVES.get(node.type)
    return convert(node) if convert else None


def _link(node: SyntaxTreeNode, children: list[MdNode]) -> list[MdNode]:
    href = str(node.attrs.get("href", ""))
    if not href.lower().startswith(_LINK_SCHEMES):
        return children
    return [MdNode("a", href=href, children=tuple(children))]


def _text(nodes: Iterable[MdNode]) -> str:
    return "".join(n.text + _text(n.children) for n in nodes)


def _whole(node: SyntaxTreeNode, children: list[MdNode]) -> bool:
    text = _text(children)
    # A one-word cell (a name, an id, a number) stays whole; WebKit breaks after a hyphen.
    if len(text.split()) == 1:
        return True
    # A table this wide scrolls on a phone anyway, and a short cell squeezed to one word per
    # line would make every row several lines tall.
    columns = len(node.parent.children) if node.parent else 0
    return columns >= WIDE_TABLE and len(text) <= SHORT_CELL


def _token_spans(text: str) -> list[MdNode]:
    parts: list[MdNode] = []
    at = 0
    for found in _BREAKABLE_RUN.finditer(text):
        if found.start() > at:
            parts.append(MdNode(text=text[at : found.start()]))
        parts.append(MdNode("span", cls="cell-token", children=(MdNode(text=found.group(0)),)))
        at = found.end()
    if at < len(text):
        parts.append(MdNode(text=text[at:]))
    return parts


def _kept_whole(nodes: Iterable[MdNode]) -> list[MdNode]:
    """Each breakable run in a prose cell's text, held to one line so the column is as wide as it."""
    out: list[MdNode] = []
    for node in nodes:
        if not node.tag:
            out += _token_spans(node.text)
        elif node.tag == "code" or node.tag in MATH_TAGS:
            out.append(node)
        else:
            out.append(replace(node, children=tuple(_kept_whole(node.children))))
    return out


def _cell_cls(node: SyntaxTreeNode, children: list[MdNode]) -> str:
    names = [_ALIGN.get(str(node.attrs.get("style", "")), "")]
    if _whole(node, children):
        names.append("cell-whole")
    return " ".join(n for n in names if n)


def _cls(node: SyntaxTreeNode, children: list[MdNode]) -> str:
    if node.tag in ("th", "td"):
        return _cell_cls(node, children)
    return "task" if node.attrs.get("class") == "task-list-item" else ""


def _element(node: SyntaxTreeNode, children: list[MdNode]) -> list[MdNode]:
    if node.type == "link":
        return _link(node, children)
    # Images are never fetched: their alt text is all that is shown.
    if node.type == "image" or node.tag not in TAGS:
        return children
    cls = _cls(node, children)
    if node.tag in ("th", "td") and "cell-whole" not in cls:
        # The geometry gate holds no cell narrower than its widest word; the table scrolls instead.
        children = _kept_whole(children)
    element = MdNode(node.tag, cls=cls, children=tuple(children))
    if node.tag == "table":
        # A table wider than the turn scrolls in this box; the page never moves sideways.
        return [MdNode("div", cls="table-scroll", children=(element,))]
    return [element]


def _convert(node: SyntaxTreeNode) -> list[MdNode]:
    if node.type == "text":
        return _text_leaf(node)
    leaf = _leaf(node)
    if leaf is not None:
        return [leaf]
    children = [part for child in node.children for part in _convert(child)]
    if node.type == "inline" or node.hidden:
        return children
    return _element(node, children)


# Every poll re-renders every turn, and a turn's text never changes once written.
@lru_cache(maxsize=512)
def render_tree(text: str) -> tuple[MdNode, ...]:
    """Parse markdown into nodes the template emits; nothing here is markup."""
    root = SyntaxTreeNode(_PARSER.parse(text))
    return tuple(part for child in root.children for part in _convert(child))
