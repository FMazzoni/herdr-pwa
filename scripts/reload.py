"""Reload and walk forward into pages in WebKit as an iPhone: each lands where it lands when opened.

WebKit restores an entry's scroll after a reload and on a forward, where Firefox, the gate's
browser, restores before the page lands; so this is a WebKit walk, for eyes rather than the gate.
"""

import argparse
import logging
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Final

ROOT: Final = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from geometry import HTMX_SETTLED, Switch, install, serve  # noqa: E402 - after the sys.path line that makes it importable
from playwright.sync_api import Page  # noqa: E402 - after the sys.path line that makes it importable
from pydantic import BaseModel, TypeAdapter  # noqa: E402 - after the sys.path line that makes it importable
from shots import IPHONE, NARROW, SETTLE_MS, Device, context, drive, missing_browser, webkit  # noqa: E402 - after the sys.path line that makes it importable

from tests.catalogue import AGENT, State, pick  # noqa: E402 - after the sys.path line that makes it importable

# Where the reader has scrolled to before the page is reloaded or walked away from.
READ_Y: Final = 100
FEED_Y: Final = 150
FILE_Y: Final = 300

PROBE: Final = """() => {
  const jump = document.getElementById("hp-jump");
  return {
    y: Math.round(scrollY),
    pinned: !!(window.hp && hp.chatend && hp.chatend.pinned()),
    pill: !!jump && !jump.hidden,
    path: location.pathname + location.search,
  };
}"""

# The furthest the page can scroll: where a page that lands itself stands.
END: Final = "() => Math.round(document.documentElement.scrollHeight - innerHeight)"

OPENED: Final = """() => location.search === "?tab=pane"
  && (!window.__hpDoorLeft || location.hash !== "") && document.readyState === "complete"
"""

# A link a person taps, pushed as the feed's own links are; fixed, so the tap scrolls nothing.
LINK: Final = """(href) => {
  const a = document.createElement("a");
  a.href = href;
  a.id = "reload-walk-link";
  a.textContent = "go";
  Object.assign(a.style, { position: "fixed", top: "60px", left: "0", zIndex: "99" });
  document.body.prepend(a);
}"""


class Spot(BaseModel):
    """Where a page stood, whether it was following its end, and whether the pill showed."""

    y: int
    pinned: bool
    pill: bool
    path: str


@dataclass(frozen=True, slots=True)
class Walk:
    """One page in one context, and whatever the walk found wrong."""

    page: Page
    base: str
    name: str
    problems: list[str] = field(default_factory=list[str])

    def settle(self, ready: str) -> Spot:
        """Wait for ``ready`` and the swaps to settle, then say where the page stands."""
        self.page.wait_for_selector(ready, state="attached")
        self.page.wait_for_function(HTMX_SETTLED)
        self.page.wait_for_timeout(SETTLE_MS * 2)
        return Spot.model_validate(self.page.evaluate(PROBE))

    def scroll(self, y: int) -> None:
        """Scroll as the reader would, and let the scroll be heard."""
        self.page.evaluate(f"() => window.scrollTo(0, {y})")
        self.page.wait_for_timeout(300)

    def tap_to(self, href: str) -> None:
        """Tap a plain link to ``href``."""
        self.page.evaluate(LINK, href)
        self.page.click("#reload-walk-link")

    def landed(self, seen: Spot, at: Spot, after: str) -> None:
        """The page must stand where it first landed, following its end, with no pill."""
        if seen.y != at.y or not seen.pinned or seen.pill:
            self.problems.append(
                f"{self.name}: after {after} {seen.path} stood at {seen.y} "
                f"(landed at {at.y}), pinned={seen.pinned}, pill={seen.pill}"
            )

    def kept(self, seen: Spot, y: int, after: str) -> None:
        """A page that does not land itself must come back where the reader left it."""
        if seen.y != y:
            self.problems.append(
                f"{self.name}: after {after} {seen.path} stood at {seen.y}, not {y}"
            )


def reloaded(walk: Walk, state: State) -> None:
    """Read up from the landing, reload: the page lands again."""
    drive(walk.page, state, walk.base + state.path)
    first = walk.settle(state.ready)
    walk.scroll(READ_Y)
    walk.page.reload()
    walk.landed(walk.settle(state.ready), first, "a reload")


def forward(walk: Walk, state: State) -> None:
    """Feed, agent, back, forward: the feed is where it was and the agent lands again."""
    walk.page.goto(walk.base + "/")
    _ = walk.settle("#feed-list")
    walk.scroll(FEED_Y)
    walk.tap_to(state.path)
    first = walk.settle(state.ready)
    walk.scroll(READ_Y)
    walk.page.go_back()
    walk.kept(walk.settle("#feed-list"), FEED_Y, "back")
    walk.page.go_forward()
    walk.landed(walk.settle(state.ready), first, "a forward")


def replaced(walk: Walk, state: State) -> None:
    """An agent page replaced by a file: the file's reload keeps the reader's place."""
    walk.page.goto(walk.base + f"{AGENT}?tab=chat")
    _ = walk.settle("#agent-view")
    walk.page.evaluate("(href) => location.replace(href)", state.path)
    _ = walk.settle(state.ready)
    walk.scroll(FILE_Y)
    walk.page.reload()
    walk.kept(walk.settle(state.ready), FILE_Y, "a reload of a page that replaced an agent page")


def pane_door(walk: Walk, state: State) -> None:
    """The answer note's link to the Pane, from ``state``'s tab: the Pane lands at its end."""
    _door(walk, state, "#answer .answer__go", "#agent-view .pane__line")


def unread_door(walk: Walk, state: State) -> None:
    """The banner's link to the Pane over a question that could not be read, whose read fails
    there too: with no line to land on, the Pane still lands at its end, the question in view."""
    _door(walk, state, "#answer .banner--held .banner__go", "#agent-view:not(:has(.boot))")


def _door(walk: Walk, state: State, door: str, view: str) -> None:
    drive(walk.page, state, walk.base + state.path)
    _ = walk.settle(door)
    length = TypeAdapter(int).validate_python(walk.page.evaluate("() => history.length"))
    walk.page.evaluate("() => { window.__hpDoorLeft = true; }")
    walk.page.click(door)
    # A new document, or this one jumped to a fragment: either way the tap has taken.
    walk.page.wait_for_function(OPENED)
    seen = walk.settle(view)
    end = TypeAdapter(int).validate_python(walk.page.evaluate(END))
    walk.landed(seen, seen.model_copy(update={"y": end}), "the Pane link")
    if walk.page.evaluate("() => history.length") != length:
        walk.problems.append(f"{walk.name}: the Pane link pushed a history entry")


# A file read from the top, not opened at a line, so only the browser can bring the reader back.
WHOLE_FILE: Final = replace(
    pick(["files-code"])[0], path=f"{AGENT}/files?path=src/app/server.py", ready=".src"
)

# A question whose rows are not buttons, so its note links to the Pane.
MULTISELECT: Final = pick(["agent-answer-multiselect"])[0]

WALKS: Final[tuple[tuple[str, State, Callable[[Walk, State], None], Device], ...]] = (
    ("reload pane question", pick(["agent-answer-multiselect"])[0], reloaded, IPHONE),
    ("reload long chat", pick(["chat-long"])[0], reloaded, IPHONE),
    ("forward into an agent", pick(["agent-answer-multiselect"])[0], forward, IPHONE),
    ("replaced by a file", WHOLE_FILE, replaced, IPHONE),
    ("pane link from chat", replace(MULTISELECT, path=f"{AGENT}?tab=chat"), pane_door, IPHONE),
    ("pane link on the pane", MULTISELECT, pane_door, IPHONE),
    # At 390px the page fits one screen, so it lands whatever the script does.
    ("pane link over an unread question", pick(["agent-unreadable"])[0], unread_door, NARROW),
)


def run(rounds: int) -> list[str]:
    """Every walk ``rounds`` times, each on a fresh app and context; the problems found."""
    switch = Switch()
    problems: list[str] = []
    with serve(switch) as base, webkit() as phone, tempfile.TemporaryDirectory() as scratch:
        for n in range(rounds):
            for name, state, walk_through, device in WALKS:
                _ = install(switch, state, Path(scratch) / f"{n}-{len(problems)}-{name}")
                ctx = context(phone, device)
                walk = Walk(ctx.new_page(), base, f"{name} #{n + 1}")
                try:
                    walk_through(walk, state)
                finally:
                    ctx.close()
                problems += walk.problems
                print(f"  {'FAIL' if walk.problems else 'ok  '} {walk.name}", flush=True)
    return problems


class Args(argparse.Namespace):
    """The command line, typed."""

    rounds: int = 3


def main(argv: list[str] | None = None) -> int:
    """Run the walks and say what went wrong."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--rounds", type=int, default=3, help="how many times to walk each")
    args = parser.parse_args(argv, namespace=Args())
    why = missing_browser()
    if why:
        print(f"reload: skipped, {why}")
        return 0
    logging.getLogger("herdr_pwa").setLevel(logging.ERROR)
    problems = run(args.rounds)
    for line in problems:
        print(f"  {line}")
    print(f"reload: {len(WALKS)} walks x {args.rounds}, {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
