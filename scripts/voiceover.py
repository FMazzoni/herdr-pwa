"""Drive the agent page in headless Firefox the way VoiceOver meets it: a poll redraws a region
while its host says it is busy, gives focus back to the control that had it, and says nothing;
a new message from the agent is said once, and so is what a tapped answer did. The stats strip
is heard as one sentence, and its redraw says nothing and leaves its sheet as the reader left it."""

from collections.abc import Callable
from pathlib import Path

from firefox import Bench, Marionette, phone_window
from settle import settle

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
from tests.waiting import eventually
from tests.webfakes import WORKING_SESSION, assistant_says, write_transcript

WIDTH = 390
HEIGHT = 844
WAIT_SECONDS = 15.0
TURNS = 12

# Records, from here on: each region drawn in and whether its host said busy as it landed,
# every settle, and every sentence put in the live region.
LISTEN = r"""
window.__hpDrawn = []; window.__hpSaid = []; window.__hpSettled = 0;
if (window.__hpListening) return true;
window.__hpListening = true;
new MutationObserver((changes) => {
  for (const change of changes) for (const node of change.addedNodes) {
    if (node.id === "agent-view" || node.id === "answer" || node.id === "agent-stats") {
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
# What VoiceOver hears on the strip's one control: its text, minus every aria-hidden subtree.
STRIP = r"""
const line = document.querySelector("#agent-stats summary");
if (!line) return null;
const parts = [];
const walker = document.createTreeWalker(line, NodeFilter.SHOW_TEXT);
for (let t = walker.nextNode(); t; t = walker.nextNode()) {
  if (!t.parentElement.closest("[aria-hidden=true]")) parts.push(t.data);
}
return {
  heard: parts.join("").replace(/\s+/g, " ").trim(),
  drawn: line.querySelectorAll(".stats__fact").length,
  open: line.parentElement.open,
};
"""
# The strip's sentence: the model, then what it says of the context, the turn and the spend.
# The strip draws four facts for that sentence: model, context, turn, output.
DRAWN_FACTS = 4
SENTENCE = "Haiku 4.5, context 62 percent used, turn running 4 minutes, 38 thousand output tokens"
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
# A write's outcome banner: its words, and the verdict the server stamped on it.
OUTCOME = r"""
const banner = document.querySelector(arguments[0]);
if (!banner) return null;
return {
  text: banner.innerText.replace(/\s+/g, " ").trim(),
  outcome: banner.getAttribute("data-hp-outcome"),
};
"""
# The verdict of a tap that landed and was seen to land, as the banner's data-hp-outcome starts.
CONFIRMED = "confirmed:"


def _heard(driver: Marionette) -> dict[str, object]:
    found = driver.script(HEARD)
    return found if isinstance(found, dict) else {}


def _until(driver: Marionette, done: Callable[[dict[str, object]], bool]) -> dict[str, object]:
    """Wait until ``done`` holds of what was heard and nothing is in flight, then let it settle."""
    if not eventually(lambda: done(seen := _heard(driver)) and not seen.get("busy"), WAIT_SECONDS):
        return {}
    settle(driver)
    return _heard(driver)


def _ready(driver: Marionette, selector: str) -> bool:
    if not eventually(lambda: bool(driver.script(READY, selector)), WAIT_SECONDS):
        return False
    settle(driver)
    return True


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
    # The live region is emptied and refilled a beat later; settling in the page outlasts it.
    settle(driver)
    said = [str(s) for s in _list(_heard(driver), "said")]
    if len(said) == 1 and said[0].startswith("New message: ") and text in said[0]:
        return []
    return [f"{name}: a new reply should be said once, as a new message; heard {said!r}"]


def poll(bench: Bench, home: Path) -> list[str]:
    """The quiet poll, then a new message, on a chat under a question."""
    driver, base = bench.driver, bench.base
    name = "voiceover poll"
    driver.send("WebDriver:Navigate", {"url": base + f"{AGENT}?tab=chat"})
    if not _ready(driver, "#agent-view .turn--agent") or not _ready(driver, "#answer .button"):
        return [f"{name}: the chat and its question never drew"]
    return _quiet_poll(driver, name) + _new_message(driver, name, home)


def outcome(bench: Bench, home: Path) -> list[str]:
    """A tapped answer's outcome banner is said through the live region."""
    del home
    driver, base = bench.driver, bench.base
    name = "voiceover outcome"
    state = next(s for s in STATES if s.name == "agent-answer-confirmed")
    driver.send("WebDriver:Navigate", {"url": base + state.path})
    if not _ready(driver, state.armed):
        return [f"{name}: the question never drew"]
    driver.script(LISTEN)
    driver.script(state.act)
    if not _ready(driver, state.ready):
        return [f"{name}: the tap never came back with its outcome"]
    # The live region is emptied and refilled a beat later; settling in the page outlasts it.
    settle(driver)
    said = [str(s) for s in _list(_heard(driver), "said")]
    banner = driver.script(OUTCOME, state.ready)
    if not isinstance(banner, dict):
        return [f"{name}: the outcome banner went away before it could be read"]
    # The write's own verdict, not its wording: a banner that reads well over a write that did not
    # land is the failure this walk exists for.
    if not str(banner.get("outcome")).startswith(CONFIRMED):
        return [f"{name}: the banner said aloud is for {banner.get('outcome')!r}, not a landed tap"]
    if said == [banner.get("text")]:
        return []
    return [f"{name}: the outcome {banner.get('text')!r} should be said once; heard {said!r}"]


def _strip_heard(driver: Marionette) -> dict[str, object]:
    found = driver.script(STRIP)
    return found if isinstance(found, dict) else {}


def strip(bench: Bench, home: Path) -> list[str]:
    """The strip is one sentence aloud; refreshed, it says nothing and keeps its sheet open."""
    del home
    driver, base = bench.driver, bench.base
    name = "voiceover strip"
    state = next(s for s in STATES if s.name == "usage-chat")
    driver.send("WebDriver:Navigate", {"url": base + state.path})
    if not _ready(driver, state.ready):
        return [f"{name}: the strip never drew"]
    problems: list[str] = []
    first = _strip_heard(driver)
    if first.get("heard") != SENTENCE:
        problems.append(
            f"{name}: the strip should say {SENTENCE!r}; it says {first.get('heard')!r}"
        )
    if _count(first, "drawn") < DRAWN_FACTS:
        problems.append(f"{name}: the drawn facts are missing, so the sentence proves nothing")
    driver.script("document.querySelector('#agent-stats summary').click();")
    driver.script(LISTEN)
    driver.script(POKE)
    seen = _until(driver, lambda s: "agent-stats" in _ids(s))
    if not seen:
        return [*problems, f"{name}: the strip was never redrawn"]
    problems += [
        f"{name}: #agent-stats was drawn in while its host did not say aria-busy"
        for d in _list(seen, "drawn")
        if isinstance(d, dict) and d.get("id") == "agent-stats" and d.get("busy") != "true"
    ]
    problems += [f"{name}: a strip redraw said {s!r}" for s in _list(seen, "said")]
    if _strip_heard(driver).get("open") is not True:
        problems.append(f"{name}: the redraw closed the sheet the reader had opened")
    return problems


def _walks() -> tuple[tuple[State, Callable[[Bench, Path], list[str]]], ...]:
    asking = State(
        "voiceover poll",
        f"{AGENT}?tab=chat",
        "",
        runner(lambda: herd(**BLOCKED), capture(MENU)),
        long_chat(TURNS),
    )
    answered = next(s for s in STATES if s.name == "agent-answer-confirmed")
    stats = next(s for s in STATES if s.name == "usage-chat")
    return ((asking, poll), (answered, outcome), (stats, strip))


def run(bench: Bench) -> list[str]:
    """Every walk on a fresh app in its own phone window; empty when VoiceOver is served well."""
    driver = bench.driver
    problems: list[str] = []
    for state, walk in _walks():
        home = bench.scratch / f"voiceover-{state.name.replace(' ', '-')}"
        home.mkdir(parents=True)
        rig = build(state, home)
        bench.mount(rig.http.app)
        first = driver.handles()[0]
        phone_window(driver, WIDTH, HEIGHT)
        try:
            problems += walk(bench, home)
        finally:
            driver.send("WebDriver:CloseWindow", {})
            driver.send("WebDriver:SwitchToWindow", {"handle": first})
        print(f"  walked {state.name}", flush=True)
    return problems
