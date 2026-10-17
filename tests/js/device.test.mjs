/* The choices this device remembers (Keep the columns, Stop the animation), in hp.js, against a
 * storage that works, one whose every call throws, and one the browser refuses to hand over. */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { runInNewContext } from "node:vm";

const HP = new URL("../../src/herdr_pwa/web/static/hp.js", import.meta.url);

function load(window) {
  window.window = window;
  runInNewContext(readFileSync(HP, "utf8"), { window, document: { addEventListener: () => {} } });
  return window.hp;
}

function memory() {
  const kept = new Map();
  return { getItem: (key) => (kept.has(key) ? kept.get(key) : null), setItem: (key, value) => kept.set(key, value) };
}

test("a choice is remembered as on and off, and is off until made", () => {
  const hp = load({ localStorage: memory() });
  assert.equal(hp.kept("hp-still"), false);
  hp.keep("hp-still", true);
  assert.equal(hp.kept("hp-still"), true);
  hp.keep("hp-still", false);
  assert.equal(hp.kept("hp-still"), false);
});

test("storage whose every call throws reads as off, and keeping into it throws nothing", () => {
  const refuse = () => {
    throw new Error("QuotaExceededError");
  };
  const hp = load({ localStorage: { getItem: refuse, setItem: refuse } });
  assert.equal(hp.kept("hp-still"), false);
  assert.doesNotThrow(() => hp.keep("hp-still", true));
});

test("storage the browser will not hand over reads as off, and keeping throws nothing", () => {
  const window = {};
  Object.defineProperty(window, "localStorage", {
    get() {
      throw new Error("SecurityError");
    },
  });
  const hp = load(window);
  assert.equal(hp.kept("hp-still"), false);
  assert.doesNotThrow(() => hp.keep("hp-still", true));
});
