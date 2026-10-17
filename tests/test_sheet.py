"""The contact sheet's old-app column comes from frozen shots, never from a checkout."""

import argparse
import sys
from pathlib import Path

# The browser scripts import each other as top-level modules, as they do when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from scripts import sheet
from scripts.shots import IPHONE


def test_the_default_left_side_is_the_frozen_folder() -> None:
    side = sheet._left(argparse.Namespace(left=None, left_label="before"), IPHONE)  # pyright: ignore[reportPrivateUsage]
    assert side.shots == sheet.FROZEN
    assert sheet.FROZEN.parts[-3:] == (".cache", "herdr-pwa-v2", "legacy-shots")


def test_a_frozen_shot_is_drawn_and_a_v2_only_key_says_why(tmp_path: Path) -> None:
    (tmp_path / "iphone").mkdir()
    (tmp_path / "iphone" / "feed.png").write_bytes(b"png")
    old = sheet.Side("old app (frozen)", tmp_path, {"files": "the old app had no screen"})
    new = sheet.Side("v2", tmp_path, {})
    page = sheet.write_sheet(["feed", "files"], old, new, tmp_path / "s.html", embed=False)
    text = page.read_text(encoding="utf-8")
    assert 'src="iphone/feed.png"' in text
    assert "the old app had no screen" in text
