/* The swap bus in hp.js, run against a stand-in document: a swap's phases run in their named
 * order whatever order their handlers were added in, and one handler's throw stops no other. */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { runInNewContext } from "node:vm";

const HP = new URL("../../src/herdr_pwa/web/static/hp.js", import.meta.url);

// hp.js on a page that records its listeners, so a test can fire them as htmx and the page would.
function load() {
  const listeners = {};
  const reported = [];
  const window = {
    reportError: (err) => reported.push(err.message),
  };
  const document = {
    addEventListener: (name, fn) => {
      (listeners[name] = listeners[name] || []).push(fn);
    },
  };
  window.window = window;
  runInNewContext(readFileSync(HP, "utf8"), { window, document });
  const fire = (name, detail) => listeners[name].forEach((fn) => fn({ detail }));
  return { hp: window.hp, fire, reported };
}

test("a swap's phases run in order, whatever order their handlers were added in", () => {
  const { hp, fire } = load();
  const ran = [];
  for (const name of ["swapped:announce", "swapped:scroll", "swapped:restore"]) {
    hp.bus.on(name, () => ran.push(name));
  }
  for (const name of ["swap:capture", "swap:veto", "swap:admit"]) {
    hp.bus.on(name, () => ran.push(name));
  }
  fire("htmx:beforeSwap", {});
  fire("htmx:afterSwap", {});
  assert.deepEqual(ran, [
    "swap:admit",
    "swap:veto",
    "swap:capture",
    "swapped:restore",
    "swapped:scroll",
    "swapped:announce",
  ]);
});

test("a capture sees what the veto decided, and is handed htmx's own detail", () => {
  const { hp, fire } = load();
  const detail = { shouldSwap: true };
  let seen = null;
  hp.bus.on("swap:capture", (d) => {
    seen = d.shouldSwap;
  });
  hp.bus.on("swap:veto", (d) => {
    d.shouldSwap = false;
  });
  fire("htmx:beforeSwap", detail);
  assert.equal(seen, false);
  assert.equal(detail.shouldSwap, false);
});

test("the first drawing runs restore, scroll and announce, in that order", () => {
  const { hp, fire } = load();
  const ran = [];
  hp.bus.on("ready:announce", () => ran.push("announce"));
  hp.bus.on("ready:scroll", () => ran.push("scroll"));
  hp.bus.on("ready:restore", () => ran.push("restore"));
  fire("DOMContentLoaded", undefined);
  assert.deepEqual(ran, ["restore", "scroll", "announce"]);
});

test("a handler that throws is reported, and the rest of the swap still runs", () => {
  const { hp, fire, reported } = load();
  const ran = [];
  hp.bus.on("swapped:restore", () => {
    throw new Error("broken restore");
  });
  hp.bus.on("swapped:restore", () => ran.push("restore"));
  hp.bus.on("swapped:announce", () => ran.push("announce"));
  fire("htmx:afterSwap", {});
  assert.deepEqual(ran, ["restore", "announce"]);
  assert.deepEqual(reported, ["broken restore"]);
});

test("a phase that does not exist is refused when the handler is added, not ignored", () => {
  const { hp } = load();
  assert.throws(() => hp.bus.on("swap:scroll", () => {}), /no such phase: swap:scroll/);
  assert.throws(() => hp.bus.on("veto", () => {}), /no such phase/);
});
