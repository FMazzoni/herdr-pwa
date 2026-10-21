"""The node every piece of rendered prose is made of: plain data, never markup."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MdNode:
    """A text leaf when ``tag`` is empty, otherwise one element of ``markdown.TAGS``.

    Attributes:
        cls: Names from ``markdown.CLASSES``, space-separated.
        attrs: MathML attributes named in ``mathml.MATH_ATTRS``.
        label: The language shown over a code block.
    """

    tag: str = ""
    text: str = ""
    href: str = ""
    children: tuple["MdNode", ...] = ()
    cls: str = ""
    attrs: tuple[tuple[str, str], ...] = ()
    label: str = ""
