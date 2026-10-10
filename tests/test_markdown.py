"""Chat prose: tables, math, highlighted code, task lists and bare links, all from closed sets."""

import re
from collections.abc import Callable

import pytest

from herdr_pwa.web import mathml
from herdr_pwa.web.assets import Assets
from herdr_pwa.web.markdown import CLASSES, TAGS, MdNode, render_tree
from herdr_pwa.web.mathml import MATH_ATTRS
from herdr_pwa.web.render import Pages

EVERYTHING = r"""# Title

| Name | Note |
|:-:|--:|
| `a11y` | a cell of running prose |

Inline $\frac{1}{2}$ and \(x^2\).

$$\sum_{i=1}^{n} i$$

\[ \begin{pmatrix} a & b \\ c & d \end{pmatrix} \]

```python
def f(x):  # note
    return "s" + 1
```

- [x] done
- [ ] open

See https://e.x/a for more.
"""


@pytest.fixture(autouse=True)
def _fresh() -> None:
    # The tree is cached by text; a test that swaps the converter must not see another's tree.
    render_tree.cache_clear()


def _walk(nodes: tuple[MdNode, ...]) -> list[MdNode]:
    found: list[MdNode] = []
    for node in nodes:
        found += [node, *_walk(node.children)]
    return found


def _html(text: str) -> str:
    template = Pages(Assets.load()).env.from_string('{% from "md.html" import md %}{{ md(nodes) }}')
    return template.render(nodes=render_tree(text))


def _first(nodes: tuple[MdNode, ...], tag: str) -> MdNode:
    return next(n for n in _walk(nodes) if n.tag == tag)


def test_every_tag_class_and_attribute_comes_from_its_closed_set() -> None:
    nodes = _walk(render_tree(EVERYTHING))
    assert {n.tag for n in nodes if n.tag} <= TAGS
    assert {c for n in nodes for c in n.cls.split()} <= CLASSES
    assert {name for n in nodes for name, _ in n.attrs} <= MATH_ATTRS
    # Every feature showed up, so the sets above were exercised, not vacuous.
    assert {"table", "math", "mfrac", "mtable", "span", "a"} <= {n.tag for n in nodes}


def test_a_table_scrolls_in_its_own_box_and_keeps_its_alignment() -> None:
    (wrap,) = render_tree("| a | b | c |\n|:-:|--:|---|\n| 1 | 2 | 3 |\n")
    assert (wrap.tag, wrap.cls) == ("div", "table-scroll")
    cells = [n for n in _walk(wrap.children) if n.tag in ("th", "td")]
    assert [c.cls.split()[0] if c.cls else "" for c in cells][:3] == [
        "align-center",
        "align-right",
        "cell-whole",
    ]
    html = _html("| a | b |\n|:-:|--:|\n| 1 | 2 |\n")
    assert '<th class="align-center cell-whole">a</th>' in html
    assert "style=" not in html


def test_a_one_word_cell_stays_whole_and_prose_wraps() -> None:
    nodes = _walk(render_tree("| Ticket | Change |\n|---|---|\n| `a11y` | words that wrap |\n"))
    classes = {_text(n): n.cls for n in nodes if n.tag in ("th", "td")}
    assert classes == {
        "Ticket": "cell-whole",
        "Change": "cell-whole",
        "a11y": "cell-whole",
        "words that wrap": "",
    }


def test_a_short_cell_in_a_wide_table_stays_on_one_line() -> None:
    header = "| a | b | c | d |\n|---|---|---|---|\n"
    nodes = _walk(render_tree(header + "| 2026-10-09 14:02 UTC | x | y | z |\n"))
    cell = next(n for n in nodes if n.tag == "td")
    assert cell.cls == "cell-whole"


def _text(node: MdNode) -> str:
    return node.text + "".join(_text(c) for c in node.children)


@pytest.mark.parametrize(
    ("source", "tags"),
    [
        (r"$\frac{a}{b}$", {"mfrac"}),
        (r"$$\sum_{i=1}^n i$$", {"msubsup"}),
        (r"\(\begin{pmatrix}1&2\\3&4\end{pmatrix}\)", {"mtable", "mtr", "mtd"}),
    ],
)
def test_inline_math_becomes_mathml(source: str, tags: set[str]) -> None:
    (paragraph,) = render_tree(f"so {source} holds")
    math = _first(paragraph.children, "math")
    assert ("display", "inline") in math.attrs
    assert tags <= {n.tag for n in _walk(math.children)}


@pytest.mark.parametrize("source", ["$$\n\\frac{1}{2}\n$$", r"\[ \sum_i i \]"])
def test_display_math_scrolls_in_its_own_box(source: str) -> None:
    (block,) = render_tree(source)
    assert (block.tag, block.cls) == ("div", "math-block")
    assert ("display", "block") in block.children[0].attrs
    assert '<div class="math-block"><math display="block">' in _html(source)


@pytest.mark.parametrize("source", [r"$\frac{a}{$", r"$\nosuchcommand x$"])
def test_math_that_cannot_convert_shows_its_source_as_code(source: str) -> None:
    (paragraph,) = render_tree(f"see {source} here")
    code = _first(paragraph.children, "code")
    assert code.text == source.strip("$")
    assert "math" not in {n.tag for n in _walk(paragraph.children)}


def test_dollar_amounts_stay_money() -> None:
    (paragraph,) = render_tree("it costs $5 and $10, or $ x $")
    assert "math" not in {n.tag for n in _walk(paragraph.children)}


@pytest.mark.parametrize(
    ("source", "absent"),
    [
        (r"$\href{javascript:alert(1)}{x}$", ("href", "javascript")),
        (r"$\style{color:red}{x}$", ("style", "color:red")),
        (r"$<script>alert(1)</script>$", ("<script", "</script")),
        (r"$\text{<b onload=x>}$", ("<b ", "onload=x>")),
        (r"$$\text{<img src=x onerror=alert(1)>}$$", ("<img",)),
    ],
)
def test_hostile_tex_stays_inert(source: str, absent: tuple[str, ...]) -> None:
    html = _html(f"a {source} b")
    for marker in absent:
        assert marker not in html
    assert {n.tag for n in _walk(render_tree(source)) if n.tag} <= TAGS


def _emits(xml: str) -> Callable[..., str]:
    def convert(latex: str, display: str = "inline") -> str:
        del latex, display
        return xml

    return convert


def test_hostile_mathml_is_cut_to_the_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    # Whatever the converter emits, only allowlisted tags and attributes reach the page.
    hostile = (
        '<math xmlns="http://www.w3.org/1998/Math/MathML" display="block" onload="x">'
        '<mi href="javascript:y" style="color:red" mathvariant="bold">a</mi>'
        '<mi xlink:href="javascript:z" xmlns:xlink="http://www.w3.org/1999/xlink">c</mi>'
        '<script>alert(1)</script><mo width="1em&quot; onclick=&quot;x">b</mo></math>'
    )
    monkeypatch.setattr(mathml, "convert", _emits(hostile))
    html = _html("$x$")
    assert re.findall(r"<(\w+)", html) == ["p", "math", "mi", "mi", "mrow", "mo"]
    for marker in ("onload", "href", "style", "javascript", "onclick", "script>"):
        assert marker not in html
    assert '<mi mathvariant="bold">a</mi>' in html


def test_a_dtd_from_the_converter_is_never_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    doctype = '<!DOCTYPE m [<!ENTITY a "aaaa">]><math><mi>&a;</mi></math>'
    monkeypatch.setattr(mathml, "convert", _emits(doctype))
    (paragraph,) = render_tree("$x$")
    assert [n.tag for n in paragraph.children] == ["code"]


def test_code_is_highlighted_by_its_fence_language_and_copies_back_whole() -> None:
    source = 'def f(x):  # note\n    return "<s>" + 1\n'
    (block,) = render_tree(f"```python\n{source}```")
    assert (block.tag, block.label) == ("pre", "python")
    assert "".join(_text(n) for n in block.children) == source
    kinds = {n.cls for n in block.children if n.tag == "span"}
    assert {"tok-keyword", "tok-function", "tok-comment", "tok-string", "tok-number"} <= kinds
    html = _html(f"```python\n{source}```")
    assert '<span class="tok-keyword">def</span>' in html
    assert "&#34;&lt;s&gt;&#34;" in html
    assert '<button type="button" class="code__copy" data-hp-copy>Copy</button>' in html


def test_shell_is_highlighted_too() -> None:
    (block,) = render_tree('```sh\nexport A=1; echo "$A"\n```')
    assert {"tok-builtin", "tok-variable", "tok-string"} & {n.cls for n in block.children}


@pytest.mark.parametrize("fence", ["```", "```no-such-language"])
def test_code_without_a_known_language_is_plain(fence: str) -> None:
    (block,) = render_tree(f"{fence}\nx = 1\n```")
    assert block.children == ()
    assert block.text == "x = 1\n"
    assert "<span" not in _html(f"{fence}\nx = 1\n```").split("<pre>")[1]


def test_task_lists_tick_without_a_control() -> None:
    (items,) = render_tree("- [x] shipped\n- [ ] pending\n")
    boxes = [n.cls for n in _walk(items.children) if n.tag == "span"]
    assert boxes == ["task-done", "task-open"]
    assert all(li.cls == "task" for li in items.children)
    html = _html("- [x] shipped\n- [ ] pending\n")
    assert "<input" not in html
    assert '<span class="task-done" role="img" aria-label="done"></span> shipped' in html


@pytest.mark.parametrize(
    ("text", "href"),
    [
        ("see https://e.x/a?b=1&c=2 now", "https://e.x/a?b=1&c=2"),
        ("see http://e.x now", "http://e.x"),
        ("see README.md and www.e.com now", ""),
        ("mail me@e.x or ftp://e.x/f now", ""),
    ],
)
def test_only_bare_http_urls_become_links(text: str, href: str) -> None:
    links = [n.href for n in _walk(render_tree(text)) if n.tag == "a"]
    assert links == ([href] if href else [])


@pytest.mark.parametrize(
    "text",
    [
        "cd $HOME/$USER now",
        "echo $PATH",
        "run $1 then",
        "cost $5\u2013$10",
        "the $foo$ flag",
        "set $HOME and $PATH",
    ],
)
def test_shell_variables_stay_text(text: str) -> None:
    (paragraph,) = render_tree(text)
    assert "math" not in {n.tag for n in _walk(paragraph.children)}
    assert _html(text) == f"<p>{text}</p>"


@pytest.mark.parametrize("source", ["$x^2$", "$x$", "$n$", "$a=b$"])
def test_single_dollar_math_that_reads_as_math_renders(source: str) -> None:
    (paragraph,) = render_tree(f"so {source} holds")
    assert "math" in {n.tag for n in _walk(paragraph.children)}
