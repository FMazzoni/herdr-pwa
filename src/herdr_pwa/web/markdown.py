"""Model prose as a tree of plain nodes from a closed tag set, for Jinja to escape."""

from dataclasses import dataclass
from typing import Final

from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode

# Raw HTML off, so a tag in model text is parsed as text and escaped like any other.
_PARSER: Final = MarkdownIt("commonmark", {"html": False}).enable(["strikethrough", "table"])

TAGS: Final = frozenset(
    {
        *(f"h{n}" for n in range(1, 7)),
        *("p", "ul", "ol", "li", "blockquote", "em", "strong", "s", "a", "code", "pre"),
        *("hr", "br", "table", "thead", "tbody", "tr", "th", "td"),
    }
)
VOID_TAGS: Final = frozenset({"hr", "br"})
_LINK_SCHEMES: Final = ("http://", "https://")
_LEAF_TEXT: Final = frozenset({"text", "html_inline", "html_block"})
_LEAF_CODE: Final = {"code_inline": "code", "fence": "pre", "code_block": "pre"}


@dataclass(frozen=True, slots=True)
class MdNode:
    """A text leaf when ``tag`` is empty, otherwise one element of ``TAGS``."""

    tag: str = ""
    text: str = ""
    href: str = ""
    children: tuple["MdNode", ...] = ()


def _leaf(node: SyntaxTreeNode) -> MdNode | None:
    if node.type in _LEAF_TEXT:
        return MdNode(text=node.content)
    if node.type == "softbreak":
        return MdNode(text="\n")
    code = _LEAF_CODE.get(node.type)
    return MdNode(code, text=node.content) if code else None


def _element(node: SyntaxTreeNode, children: list[MdNode]) -> list[MdNode]:
    if node.type == "link":
        href = str(node.attrs.get("href", ""))
        if not href.lower().startswith(_LINK_SCHEMES):
            return children
        return [MdNode("a", href=href, children=tuple(children))]
    # Images are never fetched: their alt text is all that is shown.
    if node.type == "image" or node.tag not in TAGS:
        return children
    return [MdNode(node.tag, children=tuple(children))]


def _convert(node: SyntaxTreeNode) -> list[MdNode]:
    leaf = _leaf(node)
    if leaf is not None:
        return [leaf]
    children = [part for child in node.children for part in _convert(child)]
    if node.type == "inline" or node.hidden:
        return children
    return _element(node, children)


def render_tree(text: str) -> tuple[MdNode, ...]:
    """Parse markdown into nodes the template emits; nothing here is markup."""
    root = SyntaxTreeNode(_PARSER.parse(text))
    return tuple(part for child in root.children for part in _convert(child))
