/* The decisions app.js makes, asked directly: the marked stretch of the real file is loaded on
 * its own, in a context with no page, no clock and no timers, and each function is handed what a
 * browser would have measured. Run with `node --test tests/js/`; no npm, nothing to install. */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { runInNewContext } from "node:vm";

const APP = new URL("../../src/herdr_pwa/web/static/app.js", import.meta.url);
const source = readFileSync(APP, "utf8");
const START = "// hp:logic:start";
const END = "// hp:logic:end";
const block = source.slice(source.indexOf(START), source.indexOf(END));
const logic = runInNewContext(`${block}\nlogic;`, Object.create(null));

// Objects made in the other context carry its prototypes; compare them as plain data.
const plain = (value) => JSON.parse(JSON.stringify(value));

// The pin after a run of events, from a page pinned at the top of a fresh load.
function pinAfter(events, from = { pinned: true, lastY: 0, reader: false }) {
  return plain(events.reduce((state, event) => logic.pin(state, event), from));
}

test("the logic stretch is marked once, and names nothing of the page", () => {
  assert.equal(source.split(START).length, 2);
  assert.equal(source.split(END).length, 2);
  assert.ok(source.indexOf(START) < source.indexOf(END));
  const page = /\b(window|document|navigator|location|htmx|Date|performance|setTimeout|setInterval|requestAnimationFrame|localStorage|sessionStorage|fetch|XMLHttpRequest)\b/;
  assert.equal(block.match(page), null);
});

test("pin: a scroll ending where this script left the page is not the reader's", () => {
  const state = pinAfter([{ type: "scroll", y: 0, gap: 900 }]);
  assert.deepEqual(state, { pinned: true, lastY: 0, reader: false });
});

test("pin: the reader's scroll pins at the very end and unpins anywhere else, down as well as up", () => {
  const at = { pinned: false, lastY: 500, reader: false };
  assert.deepEqual(pinAfter([{ type: "scroll", y: 1000, gap: 2 }], at), { pinned: true, lastY: 1000, reader: true });
  assert.deepEqual(pinAfter([{ type: "scroll", y: 600, gap: 3 }], at), { pinned: false, lastY: 600, reader: true });
  const pinned = { pinned: true, lastY: 500, reader: false };
  assert.equal(pinAfter([{ type: "scroll", y: 400, gap: 300 }], pinned).pinned, false);
  assert.equal(pinAfter([{ type: "scroll", y: 520, gap: 40 }], pinned).pinned, false);
});

test("pin: a move this script made is remembered, and its scroll event then claims nothing", () => {
  const state = pinAfter([
    { type: "moved", y: 340, gap: 700 },
    { type: "scroll", y: 340, gap: 700 },
  ]);
  assert.deepEqual(state, { pinned: true, lastY: 340, reader: false });
});

test("pin: landing pins only if it reached the end, since a tall question's top is not the end", () => {
  assert.equal(pinAfter([{ type: "landed", y: 2000, gap: 0 }]).pinned, true);
  assert.equal(pinAfter([{ type: "landed", y: 1500, gap: 480 }]).pinned, false);
});

test("pin: a swap that did not move the page leaves the reader's unheard scroll to be heard", () => {
  // At the end, the reader scrolls up 400px; a write's swap lands before that scroll's event.
  const atEnd = { pinned: true, lastY: 2000, reader: false };
  const state = pinAfter(
    [
      { type: "swapped", before: 1600, y: 1600, gap: 400 },
      { type: "scroll", y: 1600, gap: 400 },
    ],
    atEnd,
  );
  assert.deepEqual(state, { pinned: false, lastY: 1600, reader: true });
});

test("pin: a swap that moved the page claims that move", () => {
  const atEnd = { pinned: true, lastY: 2000, reader: false };
  assert.equal(pinAfter([{ type: "swapped", before: 2000, y: 1900, gap: 0 }], atEnd).lastY, 1900);
  // A swap whose start was never seen claims where the page is.
  assert.equal(pinAfter([{ type: "swapped", before: null, y: 1800, gap: 0 }], atEnd).lastY, 1800);
});

test("pin: a fold opened short of the end unpins; at the end it changes nothing", () => {
  assert.equal(pinAfter([{ type: "unfolded", y: 0, gap: 600 }]).pinned, false);
  assert.equal(pinAfter([{ type: "unfolded", y: 0, gap: 1 }]).pinned, true);
  assert.equal(pinAfter([{ type: "unfolded", y: 0, gap: 0 }], { pinned: false, lastY: 0, reader: false }).pinned, false);
});

test("pin: an earlier page unpins; sending and the waiting pill pin", () => {
  assert.equal(pinAfter([{ type: "earlier", y: 0, gap: 900 }]).pinned, false);
  const away = { pinned: false, lastY: 300, reader: false };
  assert.equal(pinAfter([{ type: "sent", y: 300, gap: 900 }], away).pinned, true);
  assert.equal(pinAfter([{ type: "jumped", y: 300, gap: 900 }], away).pinned, true);
});

test("pin: landing at the end pins a page the reader had left, and short of it unpins", () => {
  const away = { pinned: false, lastY: 300, reader: true };
  assert.deepEqual(pinAfter([{ type: "landed", y: 2000, gap: 1 }], away), { pinned: true, lastY: 2000, reader: false });
  assert.equal(pinAfter([{ type: "landed", y: 1500, gap: 3 }]).pinned, false);
});

test("pin: a move this script made changes where the page was left, never whether it follows", () => {
  const away = { pinned: false, lastY: 300, reader: false };
  assert.deepEqual(pinAfter([{ type: "moved", y: 2000, gap: 0 }], away), { pinned: false, lastY: 2000, reader: false });
  const following = { pinned: true, lastY: 300, reader: false };
  assert.deepEqual(pinAfter([{ type: "moved", y: 100, gap: 900 }], following), { pinned: true, lastY: 100, reader: false });
});

test("pin: only a scroll is ever the reader's; every other event says so even after one", () => {
  const read = { pinned: false, lastY: 600, reader: true };
  const others = [
    { type: "moved", y: 600, gap: 400 },
    { type: "landed", y: 600, gap: 400 },
    { type: "swapped", before: 600, y: 600, gap: 400 },
    { type: "unfolded", y: 600, gap: 400 },
    { type: "earlier", y: 600, gap: 400 },
    { type: "sent", y: 600, gap: 400 },
    { type: "jumped", y: 600, gap: 400 },
  ];
  for (const event of others) {
    assert.equal(pinAfter([event], read).reader, false, event.type);
  }
});

test("pin: a fold, an earlier page, sending and the pill claim no position, as a swap that moved nothing", () => {
  const at = { pinned: false, lastY: 700, reader: false };
  for (const type of ["unfolded", "earlier", "sent", "jumped"]) {
    assert.equal(pinAfter([{ type, y: 1234, gap: 0 }], at).lastY, 700, type);
  }
});

test("pin: an event it does not know is refused, not ignored", () => {
  assert.throws(() => logic.pin({ pinned: true, lastY: 0, reader: false }, { type: "shrug", y: 0, gap: 0 }));
});

const page = {
  scrollHeight: 3000,
  viewHeight: 844,
  scrollY: 1000,
  asking: true,
  regionHeight: 300,
  askedTop: 500,
  dockTop: 760,
  barBottom: 60,
};

test("landing: the end, when nothing is asked or the question fits between the bar and the dock", () => {
  assert.equal(logic.landing({ ...page, asking: false, regionHeight: 5000 }), 2156);
  assert.equal(logic.landing(page), 2156);
  assert.equal(logic.landing({ ...page, regionHeight: 700 }), 2156);
});

test("landing: a question taller than the room lands with its top just under the bar", () => {
  assert.equal(logic.landing({ ...page, regionHeight: 701 }), 1000 + 500 - 60);
});

test("landing: never past the end", () => {
  assert.equal(logic.landing({ ...page, regionHeight: 900, askedTop: 2000 }), 2156);
});

test("events: a stale block names its topic; anything else names none", () => {
  assert.equal(logic.staleTopic("event: stale\ndata: chat:abc"), "chat:abc");
  assert.equal(logic.staleTopic("event:stale\ndata:feed  "), "feed");
  assert.equal(logic.staleTopic("data: feed"), null);
  assert.equal(logic.staleTopic("event: stale\ndata:"), null);
  assert.equal(logic.staleTopic("event: stale"), null);
  assert.equal(logic.staleTopic(": keepalive"), null);
  assert.equal(logic.staleTopic("event: hello\ndata: feed"), null);
  assert.deepEqual(plain(logic.eventOf("data: one\ndata: two")), { kind: "message", data: "two" });
});

test("events: only whole blocks are read, and the unfinished rest waits for more", () => {
  const first = plain(logic.blocks("event: stale\ndata: a\n\nevent: stale\nda", 0));
  assert.deepEqual(first, { blocks: ["event: stale\ndata: a"], from: 22 });
  const text = "event: stale\ndata: a\n\nevent: stale\ndata: b\n\n: ping\n\n";
  assert.deepEqual(plain(logic.blocks(text, first.from)), { blocks: ["event: stale\ndata: b", ": ping"], from: text.length });
  assert.deepEqual(plain(logic.blocks(text, text.length)), { blocks: [], from: text.length });
});

test("seen: an open page says it saw a finish once, on the redraw that first shows it done", () => {
  assert.equal(logic.finishedNow("working", "done"), true);
  assert.equal(logic.finishedNow("blocked", "done"), true);
  assert.equal(logic.finishedNow("done", "done"), false);
  assert.equal(logic.finishedNow("done", "idle"), false);
  assert.equal(logic.finishedNow("working", "idle"), false);
});

test("backoff: doubles from one second to a thirty-second cap and stays there", () => {
  const waits = [];
  let ms = logic.RETRY_FIRST_MS;
  for (let i = 0; i < 8; i++) {
    const retry = logic.backoff(ms);
    waits.push(retry.wait);
    ms = retry.next;
  }
  assert.deepEqual(waits, [1000, 2000, 4000, 8000, 16000, 30000, 30000, 30000]);
});

const box = (top, bottom) => ({ top, bottom });

test("hold: the first unit wholly in view under the bar is the one held", () => {
  // Above the screen, cut by the bar, then two in view.
  const boxes = [box(-400, -10), box(20, 300), box(310, 600), box(610, 900)];
  assert.equal(logic.markAt(boxes, 60, 844), 2);
});

test("hold: a turn taller than the screen is held by being the first still on screen", () => {
  assert.equal(logic.markAt([box(-900, -100), box(-200, 2400)], 60, 844), 1);
  assert.equal(logic.markAt([box(-900, -100)], 60, 844), -1);
  assert.equal(logic.markAt([], 60, 844), -1);
});

test("hold: the same line after the swap is the one with its key nearest its old place", () => {
  // The window dropped its oldest line and a repeated line now sits on both sides.
  const keys = ["ok", "b", "ok", "c", "ok"];
  assert.equal(logic.nearest(keys, "ok", 3), 2);
  assert.equal(logic.nearest(keys, "ok", 4), 4);
  assert.equal(logic.nearest(keys, "c", 0), 3);
  assert.equal(logic.nearest(keys, "gone", 1), -1);
});

test("defer: a read waits only for a busy reader, and only in a region found again by its id", () => {
  assert.equal(logic.defers({ id: "agent-stats", polled: true }, true), true);
  assert.equal(logic.defers({ id: "feed-list", polled: true }, true), true);
  assert.equal(logic.defers({ id: "agent-view", polled: true }, false), false);
  assert.equal(logic.defers({ id: "", polled: true }, true), false);
  assert.equal(logic.defers({ id: "composer", polled: false }, true), false);
});

test("lost: a read waits out the rest of the no-reply notice's reading time, and only while it is up", () => {
  const least = logic.LOST_READ_MS;
  assert.ok(least >= 5000, "shorter than the 5s poll it has to outlast");
  assert.equal(logic.lostHold(-1), 0);
  assert.equal(logic.lostHold(0), least);
  assert.equal(logic.lostHold(least - 1500), 1500);
  assert.equal(logic.lostHold(least), 0);
  assert.equal(logic.lostHold(least + 60000), 0);
});

// The working line: its times, which verb shows when, how two verbs' glyphs pair, and the wave.
const TIMES = { sweep: 2400, hold: 6000, morph: 900 };

test("millis: app.css's times read as milliseconds, and anything else is not a time", () => {
  assert.equal(logic.millis(" 2.4s"), 2400);
  assert.equal(logic.millis("900ms"), 900);
  assert.equal(logic.millis(".5s"), 500);
  assert.ok(Number.isNaN(logic.millis("")));
  assert.ok(Number.isNaN(logic.millis("0.12em")));
});

test("verbAt: the first verb stands from the start; each later one turns out of the one before", () => {
  assert.deepEqual(plain(logic.verbAt(0, 12, TIMES)), { index: 0, from: -1, t: 1 });
  assert.deepEqual(plain(logic.verbAt(5999, 12, TIMES)), { index: 0, from: -1, t: 1 });
  assert.deepEqual(plain(logic.verbAt(6450, 12, TIMES)), { index: 1, from: 0, t: 0.5 });
  assert.deepEqual(plain(logic.verbAt(6900, 12, TIMES)), { index: 1, from: 0, t: 1 });
  // Past the last verb the line wraps to the first, turning out of the last.
  assert.deepEqual(plain(logic.verbAt(12 * 6000, 12, TIMES)), { index: 0, from: 11, t: 0 });
});

test("pairs: shared letters pair in order, so no glyph crosses another on its way", () => {
  const word = (text) => Array.from(text + "…");
  const paired = plain(logic.pairs(word("Sifting"), word("Knitting")));
  assert.equal(paired.length, 9);
  // The ellipsis always glides from the old end to the new one.
  assert.equal(paired[8], 7);
  const kept = paired.filter((i) => i >= 0);
  assert.deepEqual(kept, [...kept].sort((a, b) => a - b));
  assert.deepEqual(plain(logic.pairs(["a", "b"], ["b", "a"])).filter((i) => i >= 0).length, 1);
  assert.deepEqual(plain(logic.pairs([], ["x"])), [-1]);
});

test("morphFrame: a paired glyph glides between its places; the rest fade where they stand", () => {
  const from = [0, 10, 20];
  const to = [0, 8, 30];
  const paired = [0, -1, 2];
  const start = plain(logic.morphFrame(from, to, paired, 0));
  assert.deepEqual(start.arriving, [{ x: 0, alpha: 1 }, { x: 8, alpha: 0 }, { x: 20, alpha: 1 }]);
  assert.deepEqual(start.leaving, [null, { x: 10, alpha: 1 }, null]);
  const end = plain(logic.morphFrame(from, to, paired, 1));
  assert.deepEqual(end.arriving, [{ x: 0, alpha: 1 }, { x: 8, alpha: 1 }, { x: 30, alpha: 1 }]);
  assert.equal(end.leaving[1].alpha, 0);
  const mid = plain(logic.morphFrame(from, to, paired, 0.5));
  assert.equal(mid.arriving[2].x, 25);
});

test("wave: one soft crest crosses the verb left to right and is gone at either end", () => {
  assert.equal(logic.wave(0, 100, 0), 0);
  assert.equal(logic.wave(100, 100, 1), 0);
  // Halfway through the sweep the crest stands at the verb's middle.
  assert.equal(logic.wave(50, 100, 0.5), 1);
  assert.ok(logic.wave(20, 100, 0.3) > logic.wave(80, 100, 0.3));
  assert.ok(logic.wave(80, 100, 0.7) > logic.wave(20, 100, 0.7));
  for (let x = 0; x <= 100; x += 5) {
    const lift = logic.wave(x, 100, 0.42);
    assert.ok(lift >= 0 && lift <= 1, `${x}: ${lift}`);
  }
});

test("ease: still at both ends, halfway at the middle", () => {
  assert.equal(logic.ease(0), 0);
  assert.equal(logic.ease(1), 1);
  assert.equal(logic.ease(0.5), 0.5);
});

test("glow: across the crest the colour runs trailing edge to leading edge, and holds beyond", () => {
  // Halfway through the sweep the crest stands at the middle of a 100px verb, 35px either side.
  assert.equal(logic.glow(50, 100, 0.5), 0.5);
  assert.equal(logic.glow(15, 100, 0.5), 0);
  assert.equal(logic.glow(85, 100, 0.5), 1);
  assert.equal(logic.glow(0, 100, 0.5), 0);
  assert.equal(logic.glow(100, 100, 0.5), 1);
  // One continuous ramp: nearer the leading edge is always further along it.
  let last = -1;
  for (let x = 15; x <= 85; x += 5) {
    const side = logic.glow(x, 100, 0.5);
    assert.ok(side > last, `${x}: ${side}`);
    last = side;
  }
});
