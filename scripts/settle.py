"""Wait in the page, on the page's own clock, until it has stopped moving.

A sleep in Python races the browser: under load the page has done less by the time it ends. A
window timed inside the page is ordered with the page's own timers instead, so anything app.js
scheduled before the window opened has run by the time it closes.
"""

from typing import Final

from firefox import Marionette

# Long enough for htmx's settle (20ms), the dock's next frame and a live region's refill (50ms).
QUIET_MS: Final = 120
# app.js holds a redraw until the reader's scroll has been still for 250ms; this outlasts it.
READER_STILL_MS: Final = 300
CEILING_MS: Final = 15000

# Resolves true once nothing has changed for ``quiet`` ms: no htmx request or swap in flight, no
# scroll, no node added or removed, and the same scroll position and page size frame to frame.
SETTLE: Final = r"""
const [quiet, ceiling] = arguments;
const done = arguments[arguments.length - 1];
const busy = ".htmx-request, .htmx-swapping, .htmx-settling, .htmx-added";
const root = document.documentElement;
const look = () => [scrollX, scrollY, root.scrollHeight, root.scrollWidth,
  root.style.getPropertyValue("--dock-h"), document.readyState].join();
let mark = look();
let since = performance.now();
const moved = () => { since = performance.now(); };
const watcher = new MutationObserver(moved);
watcher.observe(document, { subtree: true, childList: true, characterData: true });
addEventListener("scroll", moved, true);
const finish = (value) => {
  clearTimeout(limit);
  watcher.disconnect();
  removeEventListener("scroll", moved, true);
  done(value);
};
const limit = setTimeout(() => finish(false), ceiling);
const frame = () => {
  const now = look();
  if (now !== mark || document.readyState !== "complete" || document.querySelector(busy)) {
    mark = now;
    moved();
  }
  if (performance.now() - since >= quiet) finish(true);
  else requestAnimationFrame(frame);
};
requestAnimationFrame(frame);
"""


def settle(driver: Marionette, quiet_ms: int = QUIET_MS) -> bool:
    """Wait until the page has been still for ``quiet_ms`` of its own time; false at the ceiling."""
    return driver.script_async(SETTLE, quiet_ms, CEILING_MS) is True
