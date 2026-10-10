"""TeX turned into MathML nodes from a closed tag and attribute set, or into its own source."""

import re
import xml.etree.ElementTree as ET
from typing import Final

from latex2mathml.converter import convert

from herdr_pwa.web.mdnode import MdNode

# MathML Core, plus menclose, which WebKit draws and latex2mathml emits for \boxed and \cancel.
MATH_TAGS: Final = frozenset(
    {
        *("math", "mrow", "mi", "mn", "mo", "ms", "mtext", "mspace", "mpadded", "mphantom"),
        *("msub", "msup", "msubsup", "munder", "mover", "munderover", "mmultiscripts"),
        *("mprescripts", "mfrac", "msqrt", "mroot", "mtable", "mtr", "mtd", "mstyle"),
        *("merror", "menclose"),
    }
)
# Layout only: no href, style, class, id, colour or event handler survives.
MATH_ATTRS: Final = frozenset(
    {
        *("display", "mathvariant", "stretchy", "fence", "separator", "form", "accent"),
        *("accentunder", "movablelimits", "largeop", "symmetric", "lspace", "rspace"),
        *("minsize", "maxsize", "linethickness", "columnalign", "rowalign", "columnspacing"),
        *("rowspacing", "width", "height", "depth", "voffset", "scriptlevel", "displaystyle"),
        "notation",
    }
)
_VALUE: Final = re.compile(r"[A-Za-z0-9.+\- %]{1,40}")
_NS: Final = "{http://www.w3.org/1998/Math/MathML}"
# Past this a formula is shown as its source: conversion cost grows with it, every poll.
TEX_LIMIT: Final = 4000


def _attrs(element: ET.Element) -> tuple[tuple[str, str], ...]:
    return tuple(
        (name, value)
        for name, value in sorted(element.attrib.items())
        if name in MATH_ATTRS and _VALUE.fullmatch(value)
    )


def _children(element: ET.Element) -> tuple[MdNode, ...]:
    parts: list[MdNode] = [MdNode(text=element.text)] if element.text else []
    for child in element:
        parts.append(_node(child))
        if child.tail:
            parts.append(MdNode(text=child.tail))
    return tuple(parts)


def _node(element: ET.Element) -> MdNode:
    tag = element.tag.removeprefix(_NS) if element.tag.startswith(_NS) else ""
    if tag not in MATH_TAGS:
        # An element outside the set keeps its content inside a neutral group.
        return MdNode("mrow", children=_children(element))
    return MdNode(tag, attrs=_attrs(element), children=_children(element))


def _unknown_command(element: ET.Element) -> bool:
    # latex2mathml passes a command it does not know through as an identifier.
    return any((e.text or "").startswith("\\") for e in element.iter(f"{_NS}mi"))


def _parse(tex: str, *, block: bool) -> MdNode | None:
    if not tex.strip() or len(tex) > TEX_LIMIT:
        return None
    try:
        xml = convert(tex, display="block" if block else "inline")
        # No DTD, entity, comment or CDATA can come from TeX; refusing "<!" keeps expat to elements.
        if "<!" in xml:
            return None
        root = ET.fromstring(xml)  # noqa: S314
    # latex2mathml raises a dozen kinds on malformed TeX, and the source is always the fallback.
    except Exception:
        return None
    if _unknown_command(root):
        return None
    return _node(root)


def math_node(tex: str, *, block: bool) -> MdNode:
    """MathML for ``tex``, or its source as inline code when it cannot be converted."""
    math = _parse(tex, block=block)
    if math is None:
        code = MdNode("code", text=tex.strip())
        return MdNode("p", children=(code,)) if block else code
    return MdNode("div", cls="math-block", children=(math,)) if block else math
