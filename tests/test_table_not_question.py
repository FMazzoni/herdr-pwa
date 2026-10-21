"""A table the agent drew in its reply is transcript, not a question, when its idle input box is below."""

from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from herdr_pwa.engine import BlockedPrompt
from herdr_pwa.panetext import idle_composer
from herdr_pwa.writes.guard import asks_a_question
from tests._fake_herd import FIXTURES, capture

# A reply's box table with questions in its last column, above Claude Code's empty input box.
TABLE = (FIXTURES / "claude" / "idle-box-table.txt").read_text(encoding="utf-8")

QUICK = settings(
    max_examples=80,
    deadline=None,
    derandomize=True,
    database=None,
    suppress_health_check=[HealthCheck.too_slow],
)

CELL = st.text(alphabet="abcdefghij klmnop?.,()", max_size=24)
WIDE = 120


def test_the_reply_table_above_an_idle_box_is_not_a_question() -> None:
    assert BlockedPrompt.parse(TABLE.split("\n✻")[0]).drawn_labels == (), "a cell is not a row"
    assert idle_composer(TABLE.splitlines())
    assert not asks_a_question(TABLE)


@st.composite
def tables(draw: st.DrawFn) -> list[str]:
    """A numbered box table of any size, drawn the way Claude Code draws one in a reply."""
    widths = draw(st.lists(st.integers(6, 30), min_size=2, max_size=4))
    rows = draw(st.integers(2, 12))

    def rule(left: str, mid: str, right: str) -> str:
        return "  " + left + mid.join("─" * w for w in widths) + right

    def row(cells: list[str]) -> str:
        return (
            "  │"
            + "│".join(f" {c[: w - 2]}".ljust(w) for w, c in zip(widths, cells, strict=True))
            + "│"
        )

    lines = [rule("┌", "┬", "┐")]
    for n in range(1, rows + 1):
        cells = [f"{n}. {draw(CELL)}", *(draw(CELL) for _ in widths[1:])]
        lines.append(row(cells))
        if draw(st.booleans()):
            lines.append(row([draw(CELL) for _ in widths]))
        lines.append(rule("├", "┼", "┤") if n < rows else rule("└", "┴", "┘"))
    return lines


@st.composite
def idle_screens(draw: st.DrawFn) -> str:
    """A table, maybe prose after it, then the empty input box and up to three status rows."""
    after = draw(
        st.lists(st.sampled_from(["", "  Which tab first?", "✻ Brewed for 2m"]), max_size=3)
    )
    status = draw(
        st.lists(
            st.sampled_from(
                ["  herdr-pwa [main] | ctx: 9%", "  ⏵⏵ auto mode on (shift+tab to cycle)"]
            ),
            max_size=3,
        )
    )
    composer = ["─" * WIDE, "\u276f" + draw(st.sampled_from(["", " "])), "─" * WIDE]
    return "\n".join([*draw(tables()), *after, *composer, *status]) + "\n"


@QUICK
@given(idle_screens())
def test_no_numbered_table_above_an_idle_box_is_a_question(screen: str) -> None:
    assert not asks_a_question(screen)


@QUICK
@given(tables())
def test_a_numbered_table_cell_is_never_an_option_row(lines: list[str]) -> None:
    assert BlockedPrompt.parse("\n".join(lines)).drawn_labels == ()


def test_a_draft_in_the_box_is_not_an_idle_box() -> None:
    drafted = TABLE.replace("\n\u276f\n", "\n\u276f half a thought\n")
    assert not idle_composer(drafted.splitlines())


# Every capture that asks: the rules above must not have opened a hole in any of them.
ASKING = sorted(p.name for p in FIXTURES.glob("capture-blocked-*") if p.is_dir())
SCREENS = sorted(
    p
    for pattern in ("capture-*/*.txt", "foreign/**/*.txt", "hermes/*.txt", "claude/*.txt")
    for p in FIXTURES.glob(pattern)
)


@pytest.mark.parametrize("name", ASKING)
@pytest.mark.parametrize("source", ["visible", "detection", "recent"])
def test_every_capture_that_asks_still_asks(name: str, source: str) -> None:
    assert asks_a_question(capture(name, source))


def _parse_asks(screen: str) -> bool:
    prompt = BlockedPrompt.parse(screen)
    return prompt.answerable or prompt.is_choice or prompt.drew_rows or prompt.asked


@pytest.mark.parametrize("path", SCREENS, ids=[str(p.relative_to(FIXTURES)) for p in SCREENS])
def test_the_idle_box_rule_clears_only_screens_the_parse_already_clears(path: Path) -> None:
    # Measured over the corpus: an empty box below never sat under a question, so no verdict moved.
    screen = path.read_text(encoding="utf-8")
    if idle_composer(screen.splitlines()):
        assert not _parse_asks(screen)
