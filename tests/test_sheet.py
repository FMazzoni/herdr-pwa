"""The contact sheet draws a saved shot beside this tree's, and says why a side has none."""

import sys
from pathlib import Path

# The browser scripts import each other as top-level modules, as they do when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from scripts import sheet


def test_a_saved_shot_is_drawn_and_a_missing_one_says_why(tmp_path: Path) -> None:
    (tmp_path / "iphone").mkdir()
    (tmp_path / "iphone" / "feed.png").write_bytes(b"png")
    before = sheet.Side("before", tmp_path)
    after = sheet.Side("after", tmp_path / "elsewhere")
    page = sheet.write_sheet(["feed"], before, after, sheet.Page(tmp_path / "s.html"))
    text = page.read_text(encoding="utf-8")
    assert 'src="iphone/feed.png"' in text
    assert "no shot at this width" in text
