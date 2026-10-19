/* A waiting question on a live page: "More keys" stays open across the answer region's redraws,
 * and the composer is held while the region says a question waits, its draft kept in the box.
 * hp.js, folds.js and drafts.js run against a stand-in page with just the pieces they touch. */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { createContext, runInContext } from "node:vm";

const STATIC = new URL("../../src/herdr_pwa/web/static/", import.meta.url);

function details(key, open) {
  return { open, getAttribute: (name) => (name === "data-hp-key" ? key : null) };
}

// An answer region: its folds, whether it holds the composer, and nothing else.
function region(folds, holds = false) {
  return {
    id: "answer",
    holds,
    folds,
    querySelectorAll: (selector) => (selector.includes("details") ? folds : []),
  };
}

function classes() {
  const on = new Set();
  return { on, toggle: (name, force) => (force ? on.add(name) : on.delete(name)), contains: (n) => on.has(n) };
}

function composer(value) {
  const attrs = { "data-hp-draft": "s1", "data-hp-open": "Prompt", "data-hp-shut": "Answer the question above first" };
  const box = { value, disabled: false, placeholder: "Prompt", getAttribute: (n) => attrs[n] ?? null, hasAttribute: (n) => n in attrs, form: null };
  const send = { disabled: false };
  const form = {
    id: "composer",
    classList: classes(),
    box,
    send,
    querySelector: (selector) => (selector.startsWith("textarea") ? box : selector.startsWith("button") ? send : null),
    querySelectorAll: (selector) => (selector.startsWith("textarea") ? [box] : []),
  };
  box.form = form;
  return form;
}

function page() {
  const listeners = {};
  const listen = (name, fn) => {
    (listeners[name] = listeners[name] || []).push(fn);
  };
  const drawn = { answer: null, composer: null };
  const document = {
    addEventListener: listen,
    getElementById: (id) => drawn[id],
    querySelector: (selector) =>
      selector === "#answer[data-hp-holds]" ? (drawn.answer && drawn.answer.holds ? drawn.answer : null) : null,
    querySelectorAll: () => [],
  };
  const window = { document, addEventListener: listen, sessionStorage: null };
  window.window = window;
  const context = createContext({ window, document });
  for (const name of ["hp.js", "folds.js", "drafts.js"]) {
    runInContext(readFileSync(new URL(name, STATIC), "utf8"), context, { filename: name });
  }
  const fire = (name, detail) => (listeners[name] || []).forEach((fn) => fn(name === "DOMContentLoaded" ? {} : { detail }));
  // htmx's two events around one swap of `target`, the new drawing put in between.
  const swap = (target, next) => {
    fire("htmx:beforeSwap", { target, elt: target, shouldSwap: true, requestConfig: { verb: "get" } });
    drawn[next.id] = next;
    fire("htmx:afterSwap", { target, elt: next, requestConfig: { verb: "get" } });
  };
  return { drawn, swap, fire };
}

test("an opened More keys is open again after the region is redrawn", () => {
  const { drawn, swap } = page();
  const before = region([details("more-keys", true)]);
  drawn.answer = before;
  const after = region([details("more-keys", false)]);
  swap(before, after);
  assert.equal(after.folds[0].open, true);
});

test("a fold the reader left closed stays closed, and a redraw of another region opens nothing", () => {
  const { drawn, swap } = page();
  const before = region([details("more-keys", false)]);
  drawn.answer = before;
  const after = region([details("more-keys", false)]);
  swap(before, after);
  assert.equal(after.folds[0].open, false);
  const opened = region([details("more-keys", true)]);
  drawn.answer = opened;
  const box = composer("");
  drawn.composer = box;
  swap(box, composer(""));
  assert.equal(opened.folds[0].open, true, "the open fold is left as it is");
});

test("the composer is held while the answer region says a question waits, its draft kept", () => {
  const { drawn, swap } = page();
  const form = composer("half a thought");
  drawn.composer = form;
  const quiet = region([]);
  drawn.answer = quiet;
  const asking = region([], true);
  swap(quiet, asking);
  assert.equal(form.classList.contains("composer--held"), true);
  assert.equal(form.box.disabled, true);
  assert.equal(form.send.disabled, true);
  assert.equal(form.box.placeholder, "Answer the question above first");
  assert.equal(form.box.value, "half a thought", "the draft stays in the box");
  swap(asking, region([]));
  assert.equal(form.classList.contains("composer--held"), false);
  assert.equal(form.box.disabled, false);
  assert.equal(form.send.disabled, false);
  assert.equal(form.box.placeholder, "Prompt");
  assert.equal(form.box.value, "half a thought", "the draft is back, never sent");
});

test("a composer drawn while a question waits is held from its first drawing", () => {
  const { drawn, fire } = page();
  drawn.answer = region([], true);
  const form = composer("");
  drawn.composer = form;
  fire("DOMContentLoaded");
  assert.equal(form.box.disabled, true);
});
