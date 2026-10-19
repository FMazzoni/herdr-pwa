"""A contact sheet: per state, a saved shot beside this tree's, both shot as the same phone."""

import argparse
import base64
import html
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from shots import DEVICES, IPHONE, Device, missing_browser, shoot  # noqa: E402

from tests.catalogue import STATES, pick  # noqa: E402

SHEETS = ROOT / ".agents" / "sheets"
TITLES = {s.name: s.title for s in STATES}


@dataclass(frozen=True)
class Side:
    """One column of the sheet: its heading and the folder of PNGs it draws from."""

    label: str
    shots: Path
    device: Device = IPHONE


@dataclass(frozen=True)
class Page:
    """Where the sheet is written, and whether its shots are inlined or linked beside it."""

    path: Path
    embed: bool = False


def _image(path: Path, *, embed: bool, page: Path) -> str:
    if embed:
        data = base64.b64encode(path.read_bytes()).decode()
        return f"data:image/png;base64,{data}"
    return os.path.relpath(path, page.parent)


def _cell(side: Side, key: str, page: Page) -> str:
    shot = side.shots / side.device.name / f"{key}.png"
    head = f"<figcaption>{html.escape(side.label)}</figcaption>"
    if not shot.is_file():
        # A before folder shot before this device existed has no picture at its width.
        folder = side.shots / side.device.name
        why = "no screen" if folder.is_dir() else "no shot at this width"
        return f'<figure class="none">{head}<p>{html.escape(why)}</p></figure>'
    src = _image(shot, embed=page.embed, page=page.path)
    full = shot.with_suffix(".full.png")
    link = src if page.embed or not full.is_file() else _image(full, embed=False, page=page.path)
    return (
        f'<figure>{head}<a href="{html.escape(link)}">'
        f'<img src="{html.escape(src)}" alt="{html.escape(side.label)} {key}" loading="lazy">'
        "</a></figure>"
    )


STYLE = """
:root { --ground: #f4f5f7; --ink: #1b2430; --ink-2: #5b6878; --card: #ffffff; --rule: #d9dee5; }
@media (prefers-color-scheme: dark) {
  :root { --ground: #0c131b; --ink: #dee8f3; --ink-2: #9baec3; --card: #141d28; --rule: #2b3947; }
}
* { box-sizing: border-box; }
body { margin: 0; padding: 16px; background: var(--ground); color: var(--ink);
  font: 14px/1.45 system-ui, -apple-system, sans-serif; }
h1 { font-size: 18px; margin: 0 0 4px; }
.meta { color: var(--ink-2); margin: 0 0 16px; font-size: 12.5px; }
section { background: var(--card); border: 1px solid var(--rule); border-radius: 10px;
  padding: 12px; margin: 0 0 16px; }
h2 { font: 600 14px/1.3 ui-monospace, Menlo, monospace; margin: 0; }
.title { color: var(--ink-2); margin: 2px 0 10px; font-size: 12.5px; }
/* Two phones side by side even at 390px: each column is half the width, never a fixed size. */
.pair { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; max-width: 900px; }
figure { margin: 0; min-width: 0; }
figcaption { font-size: 11px; text-transform: uppercase; letter-spacing: .05em; color: var(--ink-2);
  margin-bottom: 4px; }
img { display: block; width: 100%; height: auto; border: 1px solid var(--rule); border-radius: 6px; }
.none p { margin: 0; padding: 12px; border: 1px dashed var(--rule); border-radius: 6px;
  color: var(--ink-2); font-size: 12.5px; }
"""


def write_sheet(keys: list[str], left: Side, right: Side, page: Page) -> Path:
    """Write the HTML: one section per key, the two sides as a pair of phones."""
    sections = [
        f"<section><h2>{html.escape(key)}</h2>"
        f'<p class="title">{html.escape(TITLES.get(key, ""))}</p>'
        f'<div class="pair">{_cell(left, key, page)}{_cell(right, key, page)}</div></section>'
        for key in keys
    ]
    stamp = time.strftime("%Y-%m-%d %H:%M")
    size = f"{right.device.width}x{right.device.height}"
    body = (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>Phone contact sheet</title><style>{STYLE}</style></head><body>"
        f"<h1>{html.escape(left.label)} | {html.escape(right.label)}</h1>"
        f'<p class="meta">{len(keys)} states, WebKit as an iPhone ({size}), {stamp}. '
        "Tap a screen for the whole page.</p>" + "".join(sections) + "</body></html>"
    )
    page.path.parent.mkdir(parents=True, exist_ok=True)
    page.path.write_text(body, encoding="utf-8")
    return page.path


def main(argv: list[str] | None = None) -> int:
    """Shoot both sides for the named states and write the sheet."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("keys", nargs="*", help="catalogue keys (default: every state)")
    parser.add_argument("--out", type=Path, help="the sheet's HTML path")
    parser.add_argument("--embed", action="store_true", help="one self-contained file, 1x PNGs")
    parser.add_argument(
        "--left", type=Path, required=True, help="a saved shots folder (`just shots --out DIR`)"
    )
    parser.add_argument("--left-label", default="before", help="the heading for --left")
    parser.add_argument("--right-label", default="after", help="the heading for this tree")
    parser.add_argument(
        "--device",
        choices=[d.name for d in DEVICES],
        default=IPHONE.name,
        help="which phone both columns show (default: iphone, 390x844)",
    )
    args = parser.parse_args(argv)
    why = missing_browser()
    if why:
        print(f"sheet: skipped, {why}")
        return 0
    states = pick(args.keys)
    keys = [s.name for s in states]
    work = SHEETS / time.strftime("%Y%m%d-%H%M%S")
    device = next(d for d in DEVICES if d.name == args.device)
    left = Side(args.left_label, args.left, device)
    failed = shoot(states, work / "after", (device,), css_pixels=args.embed)
    right = Side(args.right_label, work / "after", device)
    page = write_sheet(keys, left, right, Page(args.out or work / "index.html", args.embed))
    for line in failed:
        print(f"  FAIL {line}")
    size = page.stat().st_size / 1_000_000
    print(f"sheet: {len(keys)} states, {size:.1f} MB: {page}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
