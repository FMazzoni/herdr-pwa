"""Photograph catalogue states in WebKit as an iPhone sees them, for eyes rather than for a gate."""

import argparse
import logging
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from geometry import HTMX_SETTLED, Switch, install, serve  # noqa: E402
from playwright.sync_api import (  # noqa: E402
    Browser,
    BrowserContext,
    Page,
    Playwright,
    sync_playwright,
)
from playwright.sync_api import Error as BrowserError  # noqa: E402

from tests.catalogue import State, pick  # noqa: E402

# Gitignored scratch shared by every worktree; shots are looked at, never committed.
OUT = ROOT / ".agents" / "shots"
READY_MS = 15_000
# Lets the last poll's swap and the fonts settle before the picture is taken.
SETTLE_MS = 400


@dataclass(frozen=True)
class Device:
    """A phone the shots are taken as: a CSS viewport, a pixel ratio, and Safari's identity."""

    name: str
    width: int
    height: int
    scale: int


# The iPhone the app is designed on, at its full standalone height rather than Playwright's
# in-browser 664px, and the narrowest iPhone still in use.
IPHONE = Device("iphone", 390, 844, 3)
NARROW = Device("narrow", 320, 568, 2)
DEVICES = (IPHONE, NARROW)
DESCRIPTOR = "iPhone 13"


def missing_browser() -> str:
    """Why WebKit cannot run here, or ``""`` when it can."""
    with sync_playwright() as p:
        if not Path(p.webkit.executable_path).exists():
            return "WebKit is not installed: run `just webkit`"
    return ""


@dataclass(frozen=True)
class Phone:
    """A running WebKit and the Playwright that knows the device descriptors."""

    playwright: Playwright
    browser: Browser


@contextmanager
def webkit() -> Iterator[Phone]:
    """A running headless WebKit, closed afterwards."""
    with sync_playwright() as p:
        browser = p.webkit.launch()
        try:
            yield Phone(p, browser)
        finally:
            browser.close()


def context(phone: Phone, device: Device, *, css_pixels: bool = False) -> BrowserContext:
    """A browser context that is ``device``: Safari on iOS, touch, its viewport and ratio."""
    options = dict(phone.playwright.devices[DESCRIPTOR])
    options.update(
        viewport={"width": device.width, "height": device.height},
        screen={"width": device.width, "height": device.height},
        device_scale_factor=1 if css_pixels else device.scale,
    )
    return phone.browser.new_context(**options)


def snap(page: Page, target: Path) -> list[Path]:
    """The first viewport, which is what the phone shows, and the whole page below it."""
    target.parent.mkdir(parents=True, exist_ok=True)
    page.wait_for_timeout(SETTLE_MS)
    first = target.with_suffix(".png")
    full = target.with_suffix(".full.png")
    page.screenshot(path=str(first))
    page.screenshot(path=str(full), full_page=True)
    return [first, full]


FORGET = "() => { try { localStorage.clear(); sessionStorage.clear(); } catch (e) {} }"


def drive(page: Page, state: State, url: str) -> None:
    """Load the state, tap through to it if it needs a tap, and wait for its proof."""
    page.goto(url)
    if state.act:
        page.wait_for_selector(state.armed, state="attached", timeout=READY_MS)
        page.wait_for_function(HTMX_SETTLED, timeout=READY_MS)
        page.evaluate(f"() => {{ {state.act} }}")
    page.wait_for_selector(state.ready, state="attached", timeout=READY_MS)
    page.wait_for_function(HTMX_SETTLED, timeout=READY_MS)


@dataclass(frozen=True)
class Stage:
    """The browser, and the one server whose app each state swaps in."""

    phone: Phone
    switch: Switch
    base: str
    css_pixels: bool = False


def _shoot_device(stage: Stage, device: Device, states: list[State], out: Path) -> list[str]:
    failed: list[str] = []
    ctx = context(stage.phone, device, css_pixels=stage.css_pixels)
    page = ctx.new_page()
    with tempfile.TemporaryDirectory(prefix="hp-shots-") as scratch:
        for state in states:
            install(stage.switch, state, Path(scratch))
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
    *,
    css_pixels: bool = False,
) -> list[str]:
    """Shoot every state on every device into ``out/<device>/``; returns what failed."""
    logging.getLogger("herdr_pwa").setLevel(logging.ERROR)
    switch = Switch()
    failed: list[str] = []
    with serve(switch) as base, webkit() as phone:
        for device in devices:
            stage = Stage(phone, switch, base, css_pixels=css_pixels)
            failed += _shoot_device(stage, device, states, out)
    return failed


def shoot_files(
    pages: dict[str, Path], out: Path, device: Device = IPHONE, *, css_pixels: bool = False
) -> None:
    """Shoot static pages already on disk, named by key, exactly as a state is shot."""
    with webkit() as phone:
        ctx = context(phone, device, css_pixels=css_pixels)
        page = ctx.new_page()
        for key, html in pages.items():
            page.goto(html.resolve().as_uri())
            snap(page, out / device.name / key)
            print(f"  shot {device.name} {key} (file)", flush=True)
        ctx.close()


def main(argv: list[str] | None = None) -> int:
    """Shoot the named states, or all of them, and say where the pictures are."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("keys", nargs="*", help="catalogue keys (default: every state)")
    parser.add_argument("--out", type=Path, default=OUT / "v2", help="where the PNGs go")
    args = parser.parse_args(argv)
    why = missing_browser()
    if why:
        print(f"shots: skipped, {why}")
        return 0
    states = pick(args.keys)
    failed = shoot(states, args.out)
    for line in failed:
        print(f"  FAIL {line}")
    print(f"shots: {len(states)} states x {len(DEVICES)} devices in {args.out}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
