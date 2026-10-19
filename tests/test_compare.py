"""The decision page is built from a data file and shots, and renders with nothing outside it."""

import re
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

# The browser scripts import each other as top-level modules, as they do when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from scripts import compare, shots

DATA = """
title = "Status row: three ways"
intro = "Which row ships — the one with ◐?"

[[area]]
id = "B"
title = "The status row"
states = ["feed-blocked", "feed-empty"]

[[area.options]]
id = "B0"
name = "Today"

[[area.options]]
id = "B1"
name = "Counts first"
css = "b1.css"
pros = ["Counts survive at 320px"]
cons = ["One more token"]
effort = "S-M"
assumption = "breaks"
assumes = "A count is never cut"
recommended = true

[[area.options]]
id = "B2"
name = "Branch"
shots = "branch-shots"
"""


def _decision(tmp_path: Path, text: str = DATA) -> compare.Decision:
    path = tmp_path / "row.toml"
    path.write_text(text, encoding="utf-8")
    return compare.load(path)


def _page(tmp_path: Path) -> str:
    decision = _decision(tmp_path)
    work = tmp_path / "row-shots"
    for folder in (work / "tree", work / "b1", tmp_path / "branch-shots"):
        (folder / "iphone").mkdir(parents=True)
        (folder / "iphone" / "feed-blocked.png").write_bytes(b"\x89PNG fake")
    page = compare.write(decision, compare.Shelf(work, "iphone"), tmp_path / "row.html")
    return page.read_text(encoding="ascii")


def test_each_option_reads_its_pictures_from_its_own_source(tmp_path: Path) -> None:
    options = _decision(tmp_path).area[0].options
    work = tmp_path / "w"
    found = [compare.source(o, work) for o in options]
    assert found == [
        compare.Source(work / "tree"),
        compare.Source(work / "b1", tmp_path / "b1.css"),
        compare.Source(tmp_path / "branch-shots", local=False),
    ]


def test_the_page_carries_its_charset_and_no_outside_file(tmp_path: Path) -> None:
    text = _page(tmp_path)
    assert text.startswith('<!doctype html><html lang="en"><head><meta charset="utf-8">')
    assert not re.search(r"""(src|href)=["']?(https?:)?//""", text)
    assert "<script" not in text
    assert "<link" not in text
    assert " style=" not in text


def test_non_ascii_is_written_as_references_so_no_host_can_garble_it(tmp_path: Path) -> None:
    text = _page(tmp_path)
    assert text.isascii()
    assert "&#9680;" in text


def test_every_option_is_pictured_with_its_tradeoffs_and_a_gap_says_why(tmp_path: Path) -> None:
    text = _page(tmp_path)
    assert text.count('src="data:image/png;base64,') == 3
    assert text.count("no shot of this state") == 3
    assert "Effort S-M" in text
    assert "Breaks: A count is never cut" in text
    assert "option option--rec" in text
    assert "Counts survive at 320px" in text


def test_an_option_with_two_sources_is_refused(tmp_path: Path) -> None:
    both = DATA.replace('shots = "branch-shots"', 'shots = "branch-shots"\ncss = "b2.css"')
    with pytest.raises(ValidationError, match="css or shots, not both"):
        _decision(tmp_path, both)


def test_an_unknown_state_is_refused_before_anything_is_shot(tmp_path: Path) -> None:
    with pytest.raises(KeyError, match="no-such-screen"):
        _decision(tmp_path, DATA.replace('"feed-empty"', '"no-such-screen"'))


def test_the_wireframe_overlay_uses_only_app_tokens() -> None:
    css = shots.wire_css()
    assert "var(--ink-3)" in css
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(", css)
