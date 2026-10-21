"""A decision page: each option shot on real catalogue states, beside its pros, cons and effort."""

import argparse
import base64
import html
import sys
import time
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

ROOT: Final = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from shots import DEVICES, Look, missing_browser, shoot, wire_css  # noqa: E402

from tests.catalogue import STATES, pick  # noqa: E402

TITLES: Final = MappingProxyType({s.name: s.title for s in STATES})
SCALE: Final = "S: a few hours. M: about a day. L: several runs."
# A claude.ai artifact holds 16MB; past this the page is too heavy to open on a phone anyway.
HEAVY_BYTES: Final = 12_000_000


class Option(BaseModel):
    """One way to build it: where its pictures come from, and what it costs and keeps."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str
    summary: str = ""
    # Laid over app.css on this tree; neither this nor `shots` means this tree as it is.
    css: Path | None = None
    # A folder saved with `just shots --out`, from any tree or branch.
    shots: Path | None = None
    pros: list[str] = Field(default_factory=list[str])
    cons: list[str] = Field(default_factory=list[str])
    effort: str = ""
    assumption: Literal["keeps", "bends", "breaks"] | None = None
    assumes: str = ""
    recommended: bool = False

    @model_validator(mode="after")
    def _one_source(self) -> Self:
        if self.css is not None and self.shots is not None:
            raise ValueError(f"option {self.id}: give css or shots, not both")
        return self


class Area(BaseModel):
    """One question on the page, its options, and the states every option is shot on."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    title: str
    note: str = ""
    pick: Literal["one", "any"] = "one"
    states: list[str] = Field(min_length=1)
    options: list[Option] = Field(min_length=1)


class Decision(BaseModel):
    """The whole data file: a title, the phone it is judged on, and its areas."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str
    intro: str = ""
    device: Literal["iphone", "wide", "narrow"] = "iphone"
    effort_scale: str = SCALE
    area: list[Area] = Field(min_length=1)


def load(path: Path) -> Decision:
    """The data file, validated, with every path in it made relative to its folder."""
    decision = Decision.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))
    pick(sorted({key for area in decision.area for key in area.states}))
    here = path.parent
    areas = [
        area.model_copy(update={"options": [_rooted(o, here) for o in area.options]})
        for area in decision.area
    ]
    return decision.model_copy(update={"area": areas})


def _rooted(option: Option, here: Path) -> Option:
    update: dict[str, object] = {}
    if option.css is not None:
        update["css"] = here / option.css
    if option.shots is not None:
        update["shots"] = here / option.shots
    return option.model_copy(update=update)


@dataclass(frozen=True)
class Source:
    """Where one option's pictures live: a saved folder, or this tree under some CSS."""

    folder: Path
    css: Path | None = None
    local: bool = True


def source(option: Option, work: Path) -> Source:
    """The folder an option's pictures are read from, and whether this run takes them."""
    if option.shots is not None:
        return Source(option.shots, local=False)
    if option.css is None:
        return Source(work / "tree")
    return Source(work / option.css.stem, option.css)


def take(decision: Decision, work: Path, *, wire: bool) -> list[str]:
    """Shoot every source this tree draws, once per source, on the union of its states."""
    wanted: dict[Source, set[str]] = {}
    for area in decision.area:
        for option in area.options:
            src = source(option, work)
            if src.local:
                wanted.setdefault(src, set()).update(area.states)
    device = next(d for d in DEVICES if d.name == decision.device)
    failed: list[str] = []
    for src, keys in wanted.items():
        layers = [src.css.read_text(encoding="utf-8")] if src.css else []
        look = Look(css_pixels=True, css="\n".join(layers + ([wire_css()] if wire else [])))
        failed += shoot(pick(sorted(keys)), src.folder, (device,), look)
    return failed


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _picture(src: Source, device: str, key: str, label: str) -> str:
    shot = src.folder / device / f"{key}.png"
    caption = f"<figcaption>{_esc(label)}</figcaption>"
    if not shot.is_file():
        why = "no shot of this state" if shot.parent.is_dir() else "no shots at this width"
        return f'<figure class="none">{caption}<p>{_esc(why)}</p></figure>'
    data = base64.b64encode(shot.read_bytes()).decode()
    alt = _esc(f"{label}, {key}")
    return f'<figure>{caption}<img src="data:image/png;base64,{data}" alt="{alt}"></figure>'


def _items(head: str, lines: Iterable[str]) -> str:
    rows = "".join(f"<li>{_esc(line)}</li>" for line in lines)
    return f"<div><h4>{head}</h4><ul>{rows}</ul></div>" if rows else ""


def _tags(option: Option) -> str:
    tags: list[str] = []
    if option.recommended:
        tags.append('<span class="tag tag--rec">Recommended</span>')
    if option.effort:
        tags.append(f'<span class="tag">Effort {_esc(option.effort)}</span>')
    if option.assumption:
        word = option.assumption.capitalize()
        note = f": {_esc(option.assumes)}" if option.assumes else ""
        tags.append(f'<span class="tag tag--{option.assumption}">{word}{note}</span>')
    return f'<p class="tags">{"".join(tags)}</p>' if tags else ""


def _card(option: Option) -> str:
    cls = "option option--rec" if option.recommended else "option"
    summary = f"<p>{_esc(option.summary)}</p>" if option.summary else ""
    pc = _items("Pros", option.pros) + _items("Cons", option.cons)
    return (
        f'<article class="{cls}" id="opt-{_esc(option.id)}">'
        f'<h3><span class="id">{_esc(option.id)}</span> {_esc(option.name)}</h3>'
        f"{_tags(option)}{summary}{f'<div class=pc>{pc}</div>' if pc else ''}</article>"
    )


@dataclass(frozen=True)
class Shelf:
    """What every picture on a page shares: the folder shots land in, and the phone."""

    work: Path
    device: str


def _state_row(area: Area, key: str, shelf: Shelf) -> str:
    figures = "".join(
        _picture(source(o, shelf.work), shelf.device, key, f"{o.id} {o.name}") for o in area.options
    )
    title = TITLES.get(key, "")
    return (
        f'<div class="state"><h4><code>{_esc(key)}</code> {_esc(title)}</h4>'
        f'<div class="phones">{figures}</div></div>'
    )


def _area(area: Area, shelf: Shelf) -> str:
    hint = "Pick one." if area.pick == "one" else "Pick any."
    note = f" {_esc(area.note)}" if area.note else ""
    rows = "".join(_state_row(area, key, shelf) for key in area.states)
    cards = "".join(_card(o) for o in area.options)
    return (
        f'<section class="area" id="area-{_esc(area.id)}">'
        f'<h2><span class="id">{_esc(area.id)}</span> {_esc(area.title)}</h2>'
        f'<p class="note">{hint}{note}</p>{rows}<div class="options">{cards}</div></section>'
    )


STYLE: Final = """
:root { color-scheme: light dark; --ground: #f4f5f7; --card: #ffffff; --ink: #1b2430;
  --ink-2: #4f5b6a; --rule: #d5dbe3; --accent: #1f5fae; --keeps: #1d7a52; --bends: #8a5a00;
  --breaks: #b3261e; --gap: 16px; --radius: 10px; }
@media (prefers-color-scheme: dark) { :root { --ground: #0c131b; --card: #141d28; --ink: #dee8f3;
  --ink-2: #9baec3; --rule: #2b3947; --accent: #6ba6e6; --keeps: #3fbf9a; --bends: #e3c169;
  --breaks: #e8705a; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--ground); color: var(--ink);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
main { max-width: 1100px; margin: 0 auto; padding: var(--gap); }
h1 { font-size: 1.5rem; line-height: 1.2; margin: 0 0 6px; }
h2 { font-size: 1.2rem; margin: 0 0 4px; }
h3 { font-size: 1rem; margin: 0 0 6px; }
h4 { font-size: 0.8rem; margin: 0 0 6px; color: var(--ink-2); font-weight: 600; }
.intro, .note, .meta { color: var(--ink-2); margin: 0 0 12px; max-width: 70ch; }
.meta { font-size: 0.85rem; }
.id { display: inline-block; min-width: 2.2em; color: var(--accent); font-variant-numeric: tabular-nums; }
.area { border-top: 1px solid var(--rule); padding-top: 20px; margin-top: 24px; }
.state { margin: 0 0 18px; }
.state code { font: 600 0.8rem ui-monospace, Menlo, monospace; color: var(--ink); }
/* Side by side at any width: each phone keeps 150px and the row scrolls inside itself. */
.phones { display: grid; grid-auto-flow: column; grid-auto-columns: minmax(150px, 1fr);
  gap: 10px; overflow-x: auto; scroll-snap-type: x mandatory; padding-bottom: 4px; }
figure { margin: 0; min-width: 0; scroll-snap-align: start; }
figcaption { font-size: 0.78rem; font-weight: 600; margin: 0 0 4px; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; }
img { display: block; width: 100%; height: auto; border: 1px solid var(--rule); border-radius: 8px; }
.none p { margin: 0; padding: 12px; border: 1px dashed var(--rule); border-radius: 8px;
  color: var(--ink-2); font-size: 0.8rem; }
.options { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 280px), 1fr));
  gap: 12px; }
.option { background: var(--card); border: 1px solid var(--rule); border-radius: var(--radius);
  padding: 12px 14px; min-width: 0; }
.option--rec { border-color: var(--accent); box-shadow: inset 3px 0 0 var(--accent); }
.option p { margin: 0 0 8px; }
.tags { display: flex; flex-wrap: wrap; gap: 4px 12px; font-size: 0.82rem; color: var(--ink-2); }
.tag--rec { color: var(--accent); font-weight: 600; }
.tag--keeps { color: var(--keeps); }
.tag--bends { color: var(--bends); }
.tag--breaks { color: var(--breaks); font-weight: 600; }
.pc { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 180px), 1fr));
  gap: 8px 16px; font-size: 0.88rem; }
.pc ul { margin: 0; padding-left: 18px; }
"""


def render(decision: Decision, shelf: Shelf) -> str:
    """The page as one string: no script, no outside file, every picture inlined."""
    device = next(d for d in DEVICES if d.name == shelf.device)
    stamp = time.strftime("%Y-%m-%d %H:%M")
    intro = f'<p class="intro">{_esc(decision.intro)}</p>' if decision.intro else ""
    meta = (
        f"Shot in WebKit as a phone {device.width}x{device.height} on the catalogue's fixtures, "
        f"{stamp}. Effort: {decision.effort_scale}"
    )
    areas = "".join(_area(a, shelf) for a in decision.area)
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_esc(decision.title)}</title><style>{STYLE}</style></head><body><main>"
        f'<h1>{_esc(decision.title)}</h1>{intro}<p class="meta">{_esc(meta)}</p>{areas}'
        "</main></body></html>"
    )


def write(decision: Decision, shelf: Shelf, out: Path) -> Path:
    """Write the page, every non-ASCII character as a reference so no host can garble it."""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(render(decision, shelf).encode("ascii", "xmlcharrefreplace"))
    return out


def main(argv: list[str] | None = None) -> int:
    """Shoot what the data file needs from this tree, then write its decision page."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("data", type=Path, help="the TOML data file (see the phone-look skill)")
    parser.add_argument("--out", type=Path, help="the page (default: beside the data file)")
    parser.add_argument("--no-shoot", action="store_true", help="reuse the shots already taken")
    parser.add_argument("--wire", action="store_true", help="wireframe: words as bars, no colour")
    args = parser.parse_args(argv)
    decision = load(args.data)
    work = args.data.parent / f"{args.data.stem}-shots"
    failed: list[str] = []
    if not args.no_shoot:
        why = missing_browser()
        if why:
            print(f"compare: skipped, {why}")
            return 0
        failed = take(decision, work, wire=args.wire)
    page = write(decision, Shelf(work, decision.device), args.out or args.data.with_suffix(".html"))
    for line in failed:
        print(f"  FAIL {line}")
    size = page.stat().st_size
    print(f"compare: {size / 1_000_000:.1f} MB: {page}")
    if size > HEAVY_BYTES:
        print("compare: too heavy to publish or open on a phone; drop states or options")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
