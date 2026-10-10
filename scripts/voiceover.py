"""Drive the agent page in headless Firefox the way VoiceOver meets it: a poll redraws a region
while its host says it is busy, gives focus back to the control that had it, and says nothing;
a new message from the agent is said once, and so is what a tapped answer did."""

import time
from collections.abc import Callable
from pathlib import Path

from firefox import Marionette, phone_window
from starlette.types import ASGIApp

from tests.catalogue import (
    AGENT,
    BLOCKED,
    MENU,
    STATES,
    State,
    build,
    capture,
    herd,
    long_chat,
    runner,
)
from tests.webfakes import WORKING_SESSION, Rig, assistant_says, write_transcript

WIDTH = 390
HEIGHT = 844
WAIT_SECONDS = 15.0
SETTLE_SECONDS = 0.5
TURNS = 12
# The live region is emptied and refilled a beat later, so the same words twice are news.
SAY_SECONDS = 1.0

# Records, from here on: each region drawn in and whether its host said busy as it landed,
# every settle, and every sentence put in the live region.
LISTEN = r"""
window.__hpDrawn = []; window.__hpSaid = []; window.__hpSettled = 0;
if (window.__hpListening) return true;
window.__hpListening = true;
new MutationObserver((changes) => {
  for (const change of changes) for (const node of change.addedNodes) {
    if (node.id === "agent-view" || node.id === "answer") {
      window.__hpDrawn.push({ id: node.id, busy: change.target.getAttribute("aria-busy") });
    }
  }
}).observe(document.body, { childList: true, subtree: true });
const live = document.getElementById("hp-live");
if (live) new MutationObserver(() => {
  if (live.textContent) window.__hpSaid.push(live.textContent);
}).observe(live, { childList: true, characterData: true, subtree: true });
document.addEventListener("htmx:afterSettle", () => { window.__hpSettled += 1; });
return !!live;
"""
HEARD = r"""
const busy = document.querySelector(".htmx-request, .htmx-swapping, .htmx-settling");
const active = document.activeElement;
return {
  drawn: window.__hpDrawn, said: window.__hpSaid, settled: window.__hpSettled, busy: !!busy,
  stillBusy: document.querySelectorAll("[aria-busy]").length,
  focus: active && active.closest("#answer") ? active.textContent.trim() : null,
  oldGone: window.__hpFocused ? !window.__hpFocused.isConnected : null,
  newest: [...document.querySelectorAll("#agent-view .turn")].map((t) => t.textContent).pop() || "",
};
"""
FOCUS = r"""
const button = document.querySelector("#answer .button--option");
if (!button) return null;
button.focus();
window.__hpFocused = button;
return document.activeElement === button ? button.textContent.trim() : null;
"""
READY = r"""
const busy = document.querySelector(".htmx-request, .htmx-swapping, .htmx-settling, .boot");
return document.readyState === "complete" && !busy && !!document.querySelector(arguments[0]);
"""
POKE = "htmx.trigger(document.body, 'hp:poke');"


def _heard(driver: Marionette) -> dict[str, object]:
    found = driver.script(HEARD)
    return found if isinstance(found, dict) else {}


def _until(driver: Marionette, done: Callable[[dict[str, object]], bool]) -> dict[str, object]:
    """Wait until ``done`` holds of what was heard and nothing is in flight, then let it settle."""
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        seen = _heard(driver)
        if done(seen) and not seen.get("busy"):
            time.sleep(SETTLE_SECONDS)
            return _heard(driver)
        time.sleep(0.1)
    return {}


def _ready(driver: Marionette, selector: str) -> bool:
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        if driver.script(READY, selector):
            time.sleep(SETTLE_SECONDS)
            return True
        time.sleep(0.1)
    return False


def _count(seen: dict[str, object], key: str) -> int:
    value = seen.get(key)
    return value if isinstance(value, int) else 0


def _list(seen: dict[str, object], key: str) -> list[object]:
    value = seen.get(key)
    return list(value) if isinstance(value, list) else []


def _quiet_poll(driver: Marionette, name: str) -> list[str]:
    """Focus an option, poll both regions, and hear nothing while focus comes back."""
    label = driver.script(FOCUS)
    if not label:
        return [f"{name}: no option in the answer region could take focus"]
    driver.script(LISTEN)
    driver.script(POKE)
    seen = _until(driver, lambda s: {"agent-view", "answer"} <= _ids(s))
    if not seen:
        return [f"{name}: the poll never redrew the chat and the answer region"]
    problems: list[str] = []
    drawn = [d for d in _list(seen, "drawn") if isinstance(d, dict)]
    problems += [
        f"{name}: #{d.get('id')} was drawn in while its host did not say aria-busy"
        for d in drawn
        if d.get("busy") != "true"
    ]
    if _count(seen, "stillBusy"):
        problems.append(f"{name}: aria-busy was left on after the poll settled")
    if seen.get("oldGone") is not True:
        problems.append(f"{name}: the focused option was never redrawn, so it proves nothing")
    elif seen.get("focus") != label:
        problems.append(
            f"{name}: focus was on {label!r} and after the poll it is on {seen.get('focus')!r}"
        )
    problems += [f"{name}: a poll with nothing new said {s!r}" for s in _list(seen, "said")]
    return problems


def _ids(seen: dict[str, object]) -> set[str]:
    return {str(d.get("id")) for d in _list(seen, "drawn") if isinstance(d, dict)}


def _new_message(driver: Marionette, name: str, home: Path) -> list[str]:
    """A reply lands at the end of the chat: it is said once, and says what it says."""
    text = "Newest reply: the migration is done."
    records = [*long_chat(TURNS), assistant_says(text, uuid="said")]
    write_transcript(home / "transcripts", WORKING_SESSION, records)
    driver.script(LISTEN)
    driver.script(POKE)
    seen = _until(driver, lambda s: text in str(s.get("newest")))
    if not seen:
        return [f"{name}: the new reply never reached the chat"]
    time.sleep(SAY_SECONDS)
    said = [str(s) for s in _list(_heard(driver), "said")]
    if len(said) == 1 and said[0].startswith("New message: ") and text in said[0]:
        return []
    return [f"{name}: a new reply should be said once, as a new message; heard {said!r}"]


def poll(driver: Marionette, base: str, rig: Rig, home: Path) -> list[str]:
    """The quiet poll, then a new message, on a chat under a question."""
    del rig
    name = "voiceover poll"
    driver.send("WebDriver:Navigate", {"url": base + f"{AGENT}?tab=chat"})
    if not _ready(driver, "#agent-view .turn--agent") or not _ready(driver, "#answer .button"):
        return [f"{name}: the chat and its question never drew"]
    return _quiet_poll(driver, name) + _new_message(driver, name, home)


def outcome(driver: Marionette, base: str, rig: Rig, home: Path) -> list[str]:
    """A tapped answer's outcome banner is said through the live region."""
    del rig, home
    name = "voiceover outcome"
    state = next(s for s in STATES if s.name == "agent-answer-confirmed")
    driver.send("WebDriver:Navigate", {"url": base + state.path})
    if not _ready(driver, state.armed):
        return [f"{name}: the question never drew"]
    driver.script(LISTEN)
    driver.script(state.act)
    if not _ready(driver, state.ready):
        return [f"{name}: the tap never came back with its outcome"]
    time.sleep(SAY_SECONDS)
    said = [str(s) for s in _list(_heard(driver), "said")]
    banner = driver.script(
        "return document.querySelector(arguments[0]).innerText.replace(/\\s+/g, ' ').trim();",
        state.ready,
    )
    if said == [banner]:
        return []
    return [f"{name}: the outcome {banner!r} should be said once; heard {said!r}"]


def _walks() -> tuple[tuple[State, Callable[[Marionette, str, Rig, Path], list[str]]], ...]:
    asking = State(
        "voiceover poll",
        f"{AGENT}?tab=chat",
        "",
        runner(lambda: herd(**BLOCKED), capture(MENU)),
        long_chat(TURNS),
    )
    answered = next(s for s in STATES if s.name == "agent-answer-confirmed")
    return ((asking, poll), (answered, outcome))


def run(
    driver: Marionette, base: str, mount: Callable[[ASGIApp], None], scratch: Path
) -> list[str]:
    """Every walk on a fresh app in its own phone window; empty when VoiceOver is served well."""
    problems: list[str] = []
    for state, walk in _walks():
        home = scratch / f"voiceover-{state.name.replace(' ', '-')}"
        home.mkdir(parents=True)
        rig = build(state, home)
        mount(rig.http.app)
        first = driver.handles()[0]
        phone_window(driver, WIDTH, HEIGHT)
        try:
            problems += walk(driver, base, rig, home)
        finally:
            driver.send("WebDriver:CloseWindow", {})
            driver.send("WebDriver:SwitchToWindow", {"handle": first})
        print(f"  walked {state.name}", flush=True)
    return problems
