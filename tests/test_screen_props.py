"""Properties of the screen checks: which keys a screen earns, the token a control carries, and render."""

from typing import NamedTuple

import pytest
from hypothesis import HealthCheck, assume, example, given, settings
from hypothesis import strategies as st

from herdr_pwa.engine import BlockedPrompt, MenuShape
from herdr_pwa.render.markdown import CLASSES, TAGS, MdNode, render_tree
from herdr_pwa.render.mathml import MATH_ATTRS
from herdr_pwa.writes.keys import allowed_keys, control_token

# Derandomized and kept off disk, so CI runs the same examples every time and leaves no database.
QUICK = settings(
    max_examples=60,
    deadline=None,
    derandomize=True,
    database=None,
    suppress_health_check=[HealthCheck.too_slow],
)

WORD = st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=2, max_size=8)
ASKED = ["Do you want to proceed?", "Which one should I use?", "Pick one"]
QUESTION = st.sampled_from([*ASKED, ""])
FOOTER = st.sampled_from(
    [
        "",
        "Enter to select · ↑/↓ to navigate · Esc to cancel",
        "Space to toggle · Enter to confirm",
        "ctrl+g to edit in vim",
        "Esc to cancel · Tab to amend",
    ]
)
SCREEN_LINE = st.text(
    alphabet=st.characters(codec="utf-8", exclude_categories=["Cs", "Cc"]), max_size=40
)


# A dialog's top rule spans the pane; a short one is text, not the box's edge.
RULE = "─" * 40


class Menu(NamedTuple):
    """A dialog drawn as a question, a numbered run with a cursor on one row, and a footer."""

    question: str
    labels: list[str]
    footer: str
    cursor: int


@st.composite
def menus(draw: st.DrawFn) -> Menu:
    box = draw(st.sampled_from(["", "", "[ ] ", "[x] "]))
    labels = [box + word for word in draw(st.lists(WORD, min_size=2, max_size=11))]
    cursor = draw(st.integers(0, len(labels) - 1))
    return Menu(draw(QUESTION), labels, draw(FOOTER), cursor)


def _screen(menu: Menu, noise: str = "") -> str:
    rows = [
        f"{'\u276f' if i == menu.cursor else ' '} {i + 1}. {label}"
        for i, label in enumerate(menu.labels)
    ]
    return "\n".join([noise, RULE, menu.question, "", *rows, "", menu.footer, ""])


def _digits_only_when_corroborated(prompt: BlockedPrompt) -> None:
    digits = {key for key in allowed_keys(prompt) if key.isdigit()}
    if digits:
        assert prompt.corroborated
        assert prompt.shape is MenuShape.SINGLE_CHOICE
        assert digits == {str(option.number) for option in prompt.options}
        assert all(1 <= int(digit) <= 9 for digit in digits)


TEN_ROWS = [f"choice{letter}" for letter in "abcdefghij"]


@QUICK
@given(menus())
# Ten rows: a tenth digit is two keystrokes, so the menu earns none.
@example(menu=Menu("Which one should I use?", TEN_ROWS, "Enter to select · ↑/↓ to navigate", 0))
def test_a_menu_offers_a_digit_only_when_the_screen_corroborates_it(menu: Menu) -> None:
    _digits_only_when_corroborated(BlockedPrompt.parse(_screen(menu)))


@QUICK
@given(st.lists(SCREEN_LINE, max_size=20))
def test_any_screen_offers_a_digit_only_when_it_corroborates_it(lines: list[str]) -> None:
    _digits_only_when_corroborated(BlockedPrompt.parse("\n".join(lines)))


@QUICK
@given(menus(), st.integers(0, 10), WORD)
# A cursor run under a box that poses no question: no options are named, yet Enter answers it.
@example(menu=Menu("Pick one", ["keep", "wipe"], "", 0), row=0, new="kept")
def test_relabelling_any_row_moves_the_token(menu: Menu, row: int, new: str) -> None:
    labels = menu.labels
    at = row % len(labels)
    assume(new != labels[at])
    edited = menu._replace(labels=[*labels[:at], new, *labels[at + 1 :]])
    before = BlockedPrompt.parse(_screen(menu))
    after = BlockedPrompt.parse(_screen(edited))
    assert control_token(before) != control_token(after)


# Transcript above the dialog's rule: what scrolls and ticks while the question stays put.
def _transcript_line(words: list[str], seconds: int) -> str:
    return f"● {' '.join(words)} {seconds}s"


NOISE = st.lists(
    st.builds(_transcript_line, st.lists(WORD, max_size=5), st.integers(0, 999)),
    max_size=4,
).map("\n".join)


@QUICK
@given(menus(), st.sampled_from(ASKED), NOISE, NOISE)
def test_the_token_ignores_what_scrolls_above_the_dialog(
    menu: Menu, question: str, one: str, other: str
) -> None:
    asked = menu._replace(question=question)
    first = BlockedPrompt.parse(_screen(asked, one))
    second = BlockedPrompt.parse(_screen(asked, other))
    if first.identity:
        assert control_token(first) == control_token(second)


@pytest.mark.xfail(
    strict=True,
    reason="a box with rows and no question takes the line above its rule as the question",
)
def test_the_token_ignores_the_transcript_above_a_box_that_poses_no_question() -> None:
    unasked = Menu("", ["Keep", "Delete"], "", 0)
    first = BlockedPrompt.parse(_screen(unasked, "● ran the tests 12s"))
    second = BlockedPrompt.parse(_screen(unasked, "● ran the tests 13s"))
    assert control_token(first) == control_token(second)


def _walk(nodes: tuple[MdNode, ...]) -> list[MdNode]:
    found: list[MdNode] = []
    for node in nodes:
        found += [node, *_walk(node.children)]
    return found


# Markdown's own punctuation, HTML, TeX and table syntax, so the parser meets every construct.
MARKUP = st.lists(
    st.sampled_from(
        [
            *"abc xyz019",
            *"#*_`~>-+=|:![](){}$\\<>/&\"'.",
            "\n\n",
            "```",
            "$$",
            "<script>",
            "- [x] ",
            "| a | b |\n|:-|-:|\n| c | d |\n",
        ]
    ),
    max_size=60,
).map("".join)


@QUICK
@given(st.one_of(MARKUP, st.text(max_size=80)))
def test_render_never_emits_a_tag_class_or_attribute_outside_the_closed_sets(source: str) -> None:
    nodes = _walk(render_tree(source))
    assert {node.tag for node in nodes if node.tag} <= TAGS
    assert {name for node in nodes for name in node.cls.split()} <= CLASSES
    assert {name for node in nodes for name, _ in node.attrs} <= MATH_ATTRS
