"""Drive the real app's history in headless Firefox: a swipe back from anywhere lands on the feed.

The gesture itself belongs to iOS and no Linux engine reproduces it; what it does, history.back(),
is the browser's, and that is what is driven here, in a top-level window.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from firefox import BrowserError, Marionette
from starlette.types import ASGIApp

from herdr_pwa.engine.client import HerdrClient
from herdr_pwa.engine.config import EngineSettings
from herdr_pwa.files.roots import root_id
from herdr_pwa.settings import Settings
from herdr_pwa.web.app import create_app
from tests._fake_herd import P3, P5, S1, S2, FakeHerd
from tests.filefakes import make_repo

WAIT_SECONDS = 15.0
# Spelled as the templates' urlencode spells it, which is also what location.pathname reports.
DOOR = f"/starting/{quote(P5)}"
LOGIN = "backnav@example.com"
# Chat | Pane | Files lives in the top bar, which never scrolls away; a tap is made where it is.
TABS = "header.topbar nav.tabs"
FILES_ON = f"{TABS} .tab--on[href$='/files']"

LOCATION = r"""
const ready = arguments[0];
return {
  path: location.pathname,
  search: location.search,
  length: history.length,
  // A tap before the deferred app.js runs is a plain link, which no person is quick enough for.
  ready: document.readyState === "complete" && (!ready || document.querySelector(ready) !== null),
  persisted: window.__hpPersisted === true,
};
"""

# Detached, so the navigation the click starts cannot unload the script that made it.
CLICK = (
    "const el = document.querySelector(arguments[0]); setTimeout(() => el.click(), 0); return !!el;"
)
CONTROLLED = "return !!(navigator.serviceWorker && navigator.serviceWorker.controller);"
# Set on the page that is about to be left; only a back-forward cache restore keeps the window.
MARK_PERSISTED = """
window.__hpPersisted = false;
window.addEventListener("pageshow", (e) => { window.__hpPersisted = e.persisted; });
"""


@dataclass
class Walk:
    """One driven journey and whatever it found wrong."""

    driver: Marionette
    base: str
    name: str
    problems: list[str] = field(default_factory=list[str])
    start: int = 0

    def where(self, ready: str = "") -> dict[str, object]:
        """Where the window is now; empty while a navigation has it unloaded."""
        try:
            found = self.driver.script(LOCATION, ready)
        except BrowserError:
            return {}
        return found if isinstance(found, dict) else {}

    def wait(self, path: str, ready: str, search: str | None = None) -> dict[str, object]:
        """Wait until the window is at ``path`` with ``ready`` drawn; say so if it never is."""
        deadline = time.monotonic() + WAIT_SECONDS
        seen: dict[str, object] = {}
        while time.monotonic() < deadline:
            seen = self.where(ready)
            here = seen.get("path") == path and (search is None or seen.get("search") == search)
            if here and seen.get("ready"):
                return seen
            time.sleep(0.1)
        self.problems.append(
            f"{self.name}: never reached {path}{search or ''} with {ready} drawn; "
            f"stuck at {seen.get('path')}{seen.get('search', '')}"
        )
        return seen

    def click(self, selector: str) -> None:
        """Tap the element ``selector`` names, as a person would."""
        if not self.driver.script(CLICK, selector):
            self.problems.append(f"{self.name}: nothing matches {selector}")

    def depth(self, seen: dict[str, object], expected: int, after: str) -> None:
        """History must be ``expected`` entries deeper than the feed it started from."""
        length = seen.get("length")
        if isinstance(length, int) and length - self.start != expected:
            self.problems.append(
                f"{self.name}: after {after}, history is {length - self.start} entries past "
                f"the feed, not {expected}: a pass-through screen pushed"
            )

    def home(self) -> None:
        """Open the feed in a fresh tab and remember how deep history was there."""
        opened = self.driver.send("WebDriver:NewWindow", {"type": "tab"})
        handle = opened.get("handle") if isinstance(opened, dict) else None
        self.driver.send("WebDriver:SwitchToWindow", {"handle": handle})
        # A tab's first load leaves about:blank's process and is never cached; a second is.
        self.driver.send("WebDriver:Navigate", {"url": self.base + "/offline"})
        self.driver.send("WebDriver:Navigate", {"url": self.base + "/"})
        seen = self.wait("/", "#feed-list .cards")
        length = seen.get("length")
        self.start = length if isinstance(length, int) else 0
        self.driver.script(MARK_PERSISTED)
        self.settled()

    def settled(self) -> None:
        """Wait out the worker's first install; a page leaving mid-install is not cached."""
        deadline = time.monotonic() + WAIT_SECONDS
        while time.monotonic() < deadline:
            if self.driver.script(CONTROLLED):
                return
            time.sleep(0.1)
        self.problems.append(f"{self.name}: the service worker never took control of the feed")

    def back_to_feed(self, after: str) -> dict[str, object]:
        """Go back once; the feed is the only acceptable landing."""
        self.driver.script("setTimeout(() => history.back(), 0);")
        landed = self.wait("/", "#feed-list", "")
        if landed.get("path") != "/":
            self.problems.append(f"{self.name}: back after {after} did not land on the feed")
        return landed


def herd() -> FakeHerd:
    """An idle agent with a page, and one at a door because it has no session yet."""
    found = FakeHerd()
    found.put(P3, S1)
    found.put(P5, "", status="blocked", launch_pending=True)
    found.add_workspace("w1")
    return found


def app_for(home: Path, fake: FakeHerd) -> ASGIApp:
    """The real app over ``fake``, its state under ``home``."""
    settings = Settings.model_validate(
        {
            "socket_path": home / "no-runner.sock",
            "state_dir": home / "state",
            "transcript_roots": (str(home / "transcripts"),),
            "tailscaled_socket": home / "no-tailscaled.sock",
            "push_notifier": False,
            "assume_login": LOGIN,
            "snapshot_cache_ttl": 0.001,
        }
    )

    def factory(engine: EngineSettings) -> HerdrClient:
        return HerdrClient(engine, transport=fake)

    return create_app(settings, factory)


def tabs_and_end(walk: Walk) -> None:
    """Feed, agent, both tabs, End and Keep it: one entry past the feed throughout."""
    walk.home()
    agent = f"/agent/{S1}"
    walk.click(f'a.card__link[href="{agent}"]')
    walk.depth(walk.wait(agent, TABS), 1, "opening an agent")
    walk.click(f'{TABS} a.tab[href$="?tab=pane"]')
    walk.depth(
        walk.wait(agent, f"{TABS} .tab--on[href$='?tab=pane']", "?tab=pane"), 1, "the Pane tab"
    )
    walk.click(f'{TABS} a.tab[href$="?tab=chat"]')
    walk.depth(
        walk.wait(agent, f"{TABS} .tab--on[href$='?tab=chat']", "?tab=chat"), 1, "the Chat tab"
    )
    walk.click("a.agent__end")
    walk.depth(walk.wait(f"{agent}/close", "form[data-hp-sheet]"), 1, "End")
    walk.click('a.leave[href="' + agent + '"]')
    walk.depth(walk.wait(agent, TABS), 1, "Keep it")
    landed = walk.back_to_feed("a tab switch")
    if not landed.get("persisted"):
        walk.problems.append(
            f"{walk.name}: the feed came back by a reload, not from the back-forward cache"
        )


def write_sheet(walk: Walk) -> None:
    """Feed, workspaces, a new-workspace sheet and its write: back is still the feed."""
    walk.home()
    walk.click('a.topbar__link[href="/workspaces"]')
    walk.depth(walk.wait("/workspaces", ".cards"), 1, "opening workspaces")
    walk.click('a[href="/workspaces/new"]')
    walk.depth(walk.wait("/workspaces/new", "form[data-hp-sheet]"), 1, "the new-workspace sheet")
    walk.driver.script("document.querySelector('input[name=label]').value = 'scratch';")
    walk.click("form[data-hp-sheet] button[type=submit]")
    walk.depth(walk.wait("/workspaces", "#w7"), 1, "making a workspace")
    walk.back_to_feed("a write")


def door(walk: Walk, fake: FakeHerd) -> None:
    """A door that forwards onward once its agent has a session must never be a back target."""
    walk.home()
    # A push, as the start form's own landing is without script.
    walk.driver.script(f"setTimeout(() => location.assign('{DOOR}'), 0);")
    walk.depth(walk.wait(DOOR, "form[data-hp-sheet]"), 1, "landing on the door")
    walk.click(f'form[action="{DOOR}/keys"] button')
    walk.depth(walk.wait(DOOR, ".banner[data-hp-sent]"), 1, "a key sent from the door")
    fake.put(P5, S2)
    walk.click(f'a[href="{DOOR}"]')
    walk.depth(walk.wait(f"/agent/{S2}", TABS), 1, "the door opening its agent")
    walk.back_to_feed("the door forwarded")


def files_tab(walk: Walk, fake: FakeHerd, home: Path) -> None:
    """The Files tab, a change, its file and back to the tab: every step replaces its entry."""
    repo = make_repo(home / "repo")
    fake.put(P3, S1, cwd=str(repo), foreground_cwd=str(repo))
    walk.home()
    agent = f"/agent/{S1}"
    base = f"/files/{root_id(repo)}"
    walk.click(f'a.card__link[href="{agent}"]')
    walk.depth(walk.wait(agent, TABS), 1, "opening an agent")
    walk.click(f'{TABS} a.tab[href$="/files"]')
    # The tab opens on Changes, under the agent's own bar with its Files tab lit.
    walk.depth(walk.wait(base, FILES_ON), 1, "the Files tab")
    walk.click('main.files .flist a.frow[href*="server.py"]')
    walk.depth(walk.wait(f"{base}/diff", f"{FILES_ON}"), 1, "opening a change")
    walk.click("main.files a.files__change")
    walk.depth(walk.wait(f"{base}/file", f"{FILES_ON}"), 1, "opening the file")
    walk.click(f"{TABS} a.tab[href$='/files']")
    walk.depth(walk.wait(base, FILES_ON), 1, "the Files tab again, from the file")
    walk.click(f'{TABS} a.tab[href$="?tab=chat"]')
    walk.depth(walk.wait(agent, f"{TABS} .tab--on[href$='?tab=chat']", "?tab=chat"), 1, "Chat")
    walk.back_to_feed("the Files tab")


WALKS: tuple[tuple[str, Callable[[Walk, FakeHerd, Path], None]], ...] = (
    ("tabs", lambda walk, _fake, _home: tabs_and_end(walk)),
    ("files", files_tab),
    ("write", lambda walk, _fake, _home: write_sheet(walk)),
    ("door", lambda walk, fake, _home: door(walk, fake)),
)


def run(
    driver: Marionette, base: str, mount: Callable[[ASGIApp], None], scratch: Path
) -> list[str]:
    """Every walk on a fresh app; the problems found, empty when back always lands on the feed."""
    problems: list[str] = []
    for name, walk_through in WALKS:
        fake = herd()
        home = scratch / f"backnav-{name}"
        home.mkdir(parents=True)
        mount(app_for(home, fake))
        walk = Walk(driver, base, f"back {name}")
        first = driver.handles()[0]
        try:
            walk_through(walk, fake, home)
        finally:
            driver.send("WebDriver:CloseWindow", {})
            driver.send("WebDriver:SwitchToWindow", {"handle": first})
        problems += walk.problems
        print(f"  walked {walk.name}", flush=True)
    return problems
