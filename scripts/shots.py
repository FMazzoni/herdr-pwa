"""Photograph catalogue states in WebKit as an iPhone sees them, for eyes rather than for a gate."""

import argparse
import logging
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

ROOT: Final = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from firefox import held  # noqa: E402 - after the sys.path line that makes it importable
from geometry import HTMX_SETTLED, Switch, install, serve  # noqa: E402 - after the sys.path line that makes it importable
from playwright.sync_api import (  # noqa: E402 - after the sys.path line that makes it importable
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Route,
    sync_playwright,
)
from playwright.sync_api import Error as BrowserError  # noqa: E402 - after the sys.path line that makes it importable

from tests.catalogue import State, pick  # noqa: E402 - after the sys.path line that makes it importable

# Gitignored scratch shared by every worktree; shots are looked at, never committed.
OUT: Final = ROOT / ".agents" / "shots"
READY_MS: Final = 15_000
# Lets the last poll's swap and the fonts settle before the picture is taken.
SETTLE_MS: Final = 400
# WebKit refuses a picture taller than this many device pixels; a long file is cut to fit.
MAX_SHOT_PX: Final = 32_000


@dataclass(frozen=True, slots=True)
class Device:
    """A phone the shots are taken as: a CSS viewport, a pixel ratio, and Safari's identity."""

    name: str
    width: int
    height: int
    scale: int


# The iPhone the app is designed on, at its full standalone height rather than Playwright's
# in-browser 664px; a Pro Max size run as a Home Screen app; and the
# narrowest iPhone still in use.
IPHONE: Final = Device("iphone", 390, 844, 3)
WIDE: Final = Device("wide", 430, 932, 3)
NARROW: Final = Device("narrow", 320, 568, 2)
DEVICES: Final = (IPHONE, WIDE, NARROW)
DESCRIPTOR: Final = "iPhone 13"


def missing_browser() -> str:
    """Why WebKit cannot run here, or ``""`` when it can."""
    with sync_playwright() as p:
        if not Path(p.webkit.executable_path).exists():
            return "WebKit is not installed: run `just webkit`"
    return ""


@dataclass(frozen=True, slots=True)
class Phone:
    """A running WebKit and the Playwright that knows the device descriptors."""

    playwright: Playwright
    browser: Browser


@contextmanager
def webkit() -> Iterator[Phone]:
    """A running headless WebKit, closed afterwards, under the same lock as every browser job."""
    with held(), sync_playwright() as p:
        browser = p.webkit.launch()
        try:
            yield Phone(p, browser)
        finally:
            browser.close()


# The wireframe overlay: words become bars and colour goes grey, so only the layout reads.
WIRE_CSS: Final = Path(__file__).with_name("wire.css")


@dataclass(frozen=True, slots=True)
class Look:
    """How a shot is taken beyond the device: at CSS pixels, and with CSS laid over app.css."""

    css_pixels: bool = False
    # Appended to the served app.css, so a variant is drafted without touching the tree.
    css: str = ""


# The look every plain shot has: the device's own ratio, app.css as served.
PLAIN: Final = Look()


def wire_css() -> str:
    """The wireframe overlay's CSS."""
    return WIRE_CSS.read_text(encoding="utf-8")


def context(phone: Phone, device: Device, look: Look = PLAIN) -> BrowserContext:
    """A browser context that is ``device``: Safari on iOS, touch, its viewport and ratio."""
    options = dict(phone.playwright.devices[DESCRIPTOR])
    options.update(
        viewport={"width": device.width, "height": device.height},
        screen={"width": device.width, "height": device.height},
        device_scale_factor=1 if look.css_pixels else device.scale,
    )
    # A request a service worker answers never reaches a route, so an overlay needs it gone.
    if look.css:
        options.update(service_workers="block")
    ctx = phone.browser.new_context(**options)
    if look.css:
        ctx.route("**/static/app.css*", lambda route: _overlay(route, look.css))
    return ctx


def _overlay(route: Route, css: str) -> None:
    served = route.fetch()
    route.fulfill(response=served, body=served.text() + "\n" + css)


def snap(page: Page, target: Path) -> list[Path]:
    """The first viewport, which is what the phone shows, and the whole page below it."""
    target.parent.mkdir(parents=True, exist_ok=True)
    page.wait_for_timeout(SETTLE_MS)
    first = target.with_suffix(".png")
    full = target.with_suffix(".full.png")
    page.screenshot(path=str(first))
    tall = int(page.evaluate("() => document.documentElement.scrollHeight"))
    scale = float(page.evaluate("() => window.devicePixelRatio")) or 1.0
    width = int(page.evaluate("() => document.documentElement.clientWidth"))
    if tall * scale <= MAX_SHOT_PX:
        page.screenshot(path=str(full), full_page=True)
    else:
        clip = {"x": 0, "y": 0, "width": width, "height": int(MAX_SHOT_PX / scale)}
        page.screenshot(path=str(full), full_page=True, clip=clip)  # pyright: ignore[reportArgumentType] - playwright wants a FloatRect, and this dict is one
    return [first, full]


FORGET: Final = "() => { try { localStorage.clear(); sessionStorage.clear(); } catch (e) {} }"


def drive(page: Page, state: State, url: str) -> None:
    """Load the state, tap through to it if it needs a tap, and wait for its proof."""
    page.goto(url)
    if state.act:
        page.wait_for_selector(state.armed, state="attached", timeout=READY_MS)
        page.wait_for_function(HTMX_SETTLED, timeout=READY_MS)
        page.evaluate(f"() => {{ {state.act} }}")
    page.wait_for_selector(state.ready, state="attached", timeout=READY_MS)
    # A state held mid-tap never settles; its proof is the picture.
    if not state.in_flight:
        page.wait_for_function(HTMX_SETTLED, timeout=READY_MS)


@dataclass(frozen=True, slots=True)
class Stage:
    """The browser, and the one server whose app each state swaps in."""

    phone: Phone
    switch: Switch
    base: str
    look: Look = PLAIN


def _shoot_device(stage: Stage, device: Device, states: list[State], out: Path) -> list[str]:
    failed: list[str] = []
    ctx = context(stage.phone, device, stage.look)
    page = ctx.new_page()
    with tempfile.TemporaryDirectory(prefix="hp-shots-") as scratch:
        for state in states:
            failed += [
                f"{device.name} {state.name}: {p}"
                for p in install(stage.switch, state, Path(scratch))
            ]
            try:
                drive(page, state, stage.base + state.path)
            except BrowserError as exc:
                # The picture is still taken: a screen that never got ready is worth seeing.
                failed.append(f"{device.name} {state.name}: {str(exc).splitlines()[0]}")
            snap(page, out / device.name / state.name)
            # One page serves every state, so a remembered choice must not leak into the next.
            page.evaluate(FORGET)
            print(f"  shot {device.name} {state.name}", flush=True)
    ctx.close()
    return failed


def shoot(
    states: list[State],
    out: Path,
    devices: tuple[Device, ...] = DEVICES,
    look: Look = PLAIN,
) -> list[str]:
    """Shoot every state on every device into ``out/<device>/``; returns what failed."""
    logging.getLogger("herdr_pwa").setLevel(logging.ERROR)
    switch = Switch()
    failed: list[str] = []
    with serve(switch) as base, webkit() as phone:
        for device in devices:
            stage = Stage(phone, switch, base, look)
            failed += _shoot_device(stage, device, states, out)
    return failed


def main(argv: list[str] | None = None) -> int:
    """Shoot the named states, or all of them, and say where the pictures are."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("keys", nargs="*", help="catalogue keys (default: every state)")
    parser.add_argument("--out", type=Path, default=OUT / "current", help="where the PNGs go")
    parser.add_argument(
        "--css", type=Path, action="append", default=[], help="CSS laid over app.css (repeatable)"
    )
    parser.add_argument("--wire", action="store_true", help="wireframe: words as bars, no colour")
    args = parser.parse_args(argv)
    why = missing_browser()
    if why:
        print(f"shots: skipped, {why}")
        return 0
    states = pick(args.keys)
    layers = [p.read_text(encoding="utf-8") for p in args.css] + ([wire_css()] if args.wire else [])
    failed = shoot(states, args.out, look=Look(css="\n".join(layers)))
    for line in failed:
        print(f"  FAIL {line}")
    print(f"shots: {len(states)} states x {len(DEVICES)} devices in {args.out}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
