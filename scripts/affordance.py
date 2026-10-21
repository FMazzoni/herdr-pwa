"""Every affordance does what it looks like: a grabber drags its sheet shut, a chevron toggles.

Run on every catalogue state at every size by the geometry gate, after the state is measured,
since what it does to the page is the last thing done there.
"""

from typing import Final

from firefox import Marionette

# A grabber is the class that draws one: the short bar a sheet wears on its top edge.
GRABBER: Final = '[class*="grabber"]'
# A chevron is app.css's --chevron glyph, drawn as a pseudo-element or as its own span.
CHEVRON_GLYPH: Final = "\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK}"
# How long a swiped sheet may take to close: its settle runs well inside this.
CLOSE_MS: Final = 1500
# A swipe as a thumb makes one: down this far, in this many moves, one frame apart.
SWIPE_PX: Final = 240
SWIPE_STEPS: Final = 8
FRAME_MS: Final = 16

# The pointer events a touch makes, sent where the finger is: a touch's moves go to the
# element it began on (Pointer Events 3, implicit capture), so every event is sent there.
PROBE: Final = r"""
const [grabberSel, glyph, closeMs, swipePx, steps, frameMs] = arguments;
const done = arguments[arguments.length - 1];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const name = (el) => {
  const cls = typeof el.className === "string" ? el.className.trim().split(/\s+/).filter(Boolean) : [];
  return el.tagName.toLowerCase() + (cls.length ? "." + cls.join(".") : "");
};
const shown = (el) => {
  const r = el.getBoundingClientRect();
  return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== "hidden";
};
const centre = (el) => {
  const r = el.getBoundingClientRect();
  return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
};
const inView = (p) => p.x >= 0 && p.y >= 0 && p.x < innerWidth && p.y < innerHeight;
// Under a sheet or off the screen, nothing can be tapped, so nothing is promised.
const reachable = (el) => {
  const p = centre(el);
  const hit = inView(p) ? document.elementFromPoint(p.x, p.y) : null;
  return !!hit && (hit === el || el.contains(hit));
};
const pointer = (type, el, x, y) => el.dispatchEvent(new PointerEvent(type, {
  bubbles: true, cancelable: true, composed: true, pointerId: 41, pointerType: "touch",
  isPrimary: true, clientX: x, clientY: y, button: type === "pointermove" ? -1 : 0,
  buttons: type === "pointerup" ? 0 : 1,
}));
const glyphed = (el) => ["::before", "::after"].some((at) => getComputedStyle(el, at).content.includes(glyph))
  || (el.children.length === 0 && el.textContent.trim() === glyph);

async function chevrons(found) {
  const marked = [...document.querySelectorAll("body *")].filter((el) => shown(el) && glyphed(el));
  for (const el of marked) {
    const owner = el.closest("summary, a[href], button, select, label");
    if (!owner) {
      found.push(`the chevron on ${name(el)} marks nothing that takes a tap`);
      continue;
    }
    if (owner.tagName !== "SUMMARY" || !reachable(owner)) {
      continue;
    }
    const details = owner.parentElement;
    const was = details.open;
    owner.click();
    await wait(0);
    const toggled = details.open !== was;
    if (toggled) {
      owner.click();
      await wait(0);
    } else {
      found.push(`a tap on ${name(owner)} leaves its ${was ? "open" : "closed"} details as it was; its chevron says it toggles`);
    }
  }
}

async function grabbers(found) {
  for (const el of [...document.querySelectorAll(grabberSel)].filter(shown)) {
    const sheet = el.closest("details");
    if (!sheet || !sheet.open) {
      found.push(`the grabber ${name(el)} is drawn outside any open sheet`);
      continue;
    }
    const at = centre(el);
    pointer("pointerdown", el, at.x, at.y);
    for (let i = 1; i <= steps; i++) {
      await wait(frameMs);
      pointer("pointermove", el, at.x, at.y + (swipePx * i) / steps);
    }
    pointer("pointerup", el, at.x, at.y + swipePx);
    const started = Date.now();
    while (sheet.open && Date.now() - started < closeMs) {
      await wait(frameMs);
    }
    if (sheet.open) {
      found.push(`the grabber ${name(el)} does not close its sheet on a ${swipePx}px swipe down`);
    }
  }
}

(async () => {
  const found = [];
  try {
    // Grabbers first: a tap on a chevron behind an open sheet closes the sheet, as a tap outside does.
    await grabbers(found);
    await chevrons(found);
  } catch (err) {
    found.push(`the affordance probe threw: ${err}`);
  }
  done(found);
})();
"""


def check(driver: Marionette) -> list[str]:
    """Swipe every visible grabber and tap every reachable chevron; what did not do as drawn."""
    found = driver.script_async(
        PROBE, GRABBER, CHEVRON_GLYPH, CLOSE_MS, SWIPE_PX, SWIPE_STEPS, FRAME_MS
    )
    return [str(f) for f in found] if isinstance(found, list) else ["the affordance probe died"]
