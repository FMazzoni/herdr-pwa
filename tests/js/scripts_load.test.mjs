/* Every script the page runs, loaded in the page's order against a stand-in page that answers
 * anything: each one's top level, the first drawing and a swap run without naming something no
 * script defines. A section moved out of one file can keep a call to a function left behind, and
 * nothing else fails: the browser reports the throw and the walks still pass. */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { createContext, runInContext } from "node:vm";

const STATIC = new URL("../../src/herdr_pwa/web/static/", import.meta.url);
const ASSETS = readFileSync(new URL("../../src/herdr_pwa/web/assets.py", import.meta.url), "utf8");
const SCRIPTS = [...ASSETS.match(/SCRIPTS: Final = \(([^)]*)\)/)[1].matchAll(/"([^"]+)"/g)].map(
  (m) => m[1],
);

// Globals a browser has and node lacks or must not run here (a real fetch, a real timer).
const BROWSER = new Set([
  "navigator", "location", "history", "fetch", "XMLHttpRequest", "Notification",
  "IntersectionObserver", "ResizeObserver", "performance", "setTimeout", "setInterval",
  "clearTimeout", "clearInterval", "requestAnimationFrame", "matchMedia", "atob",
  "sessionStorage", "localStorage", "getComputedStyle", "CustomEvent",
]);

// A value that is every page object at once: any property, call or construction gives another.
function anything() {
  const stand = new Proxy(function () {}, {
    get: (_target, key) => (key === Symbol.toPrimitive ? () => 0 : stand),
    apply: () => stand,
    construct: () => stand,
    set: () => true,
  });
  return stand;
}

function page() {
  const listeners = {};
  const any = anything();
  const listen = (name, fn) => {
    (listeners[name] = listeners[name] || []).push(fn);
  };
  const own = (fields) =>
    new Proxy(fields, {
      get: (target, key) => (key in target ? target[key] : any),
    });
  const document = own({ addEventListener: listen, hidden: false });
  // Frames are kept, not run, so a test can run them where a browser could: between two scripts.
  const frames = [];
  const window = own({
    addEventListener: listen,
    document,
    requestAnimationFrame: (fn) => frames.push(fn),
  });
  window.window = window;
  // The global scope: the page's own names, the browser's as stand-ins, the language's for real,
  // and nothing else, so a name no script defines is a ReferenceError as it is on the phone.
  const fields = { window, document };
  const global = new Proxy(fields, {
    has: (target, key) => key in target || BROWSER.has(key) || key in globalThis,
    get: (target, key) => {
      if (key in target) {
        return target[key];
      }
      if (BROWSER.has(key)) {
        return any;
      }
      // node's vm asks `get`, not `has`, for a bare name, so the ReferenceError is raised here.
      if (typeof key === "string" && !(key in globalThis)) {
        throw new ReferenceError(`${key} is not defined`);
      }
      return globalThis[key];
    },
  });
  return { context: createContext(global), listeners, window, any, frames };
}

test("the script list names files that exist, hp.js first and app.js last", () => {
  assert.equal(SCRIPTS[0], "hp.js");
  assert.equal(SCRIPTS.at(-1), "app.js");
  for (const name of SCRIPTS) {
    assert.ok(readFileSync(new URL(name, STATIC), "utf8").length > 0, name);
  }
});

test("every script loads, draws and swaps without naming what no script defines", () => {
  const { context, listeners, window, any } = page();
  const reported = [];
  for (const name of SCRIPTS) {
    try {
      runInContext(readFileSync(new URL(name, STATIC), "utf8"), context, { filename: name });
    } catch (err) {
      reported.push(`${name} top level: ${err}`);
    }
    if (name === "hp.js") {
      window.reportError = (err) => reported.push(`handler: ${err}`);
    }
  }
  for (const event of ["DOMContentLoaded", "htmx:beforeSwap", "htmx:afterSwap"]) {
    for (const fn of listeners[event] || []) {
      try {
        fn({ detail: { shouldSwap: true, target: any, elt: any, requestConfig: any, xhr: any } });
      } catch (err) {
        reported.push(`${event}: ${err}`);
      }
    }
  }
  assert.deepEqual(
    reported.filter((line) => line.includes("ReferenceError")),
    [],
  );
});

// What a browser may do while the next deferred script is still on its way.
const BETWEEN = ["resize", "focusin", "focusout"];

test("an event or a frame between two scripts never reaches into a script not run yet", () => {
  const { context, listeners, any, frames } = page();
  const reported = [];
  for (const name of SCRIPTS) {
    runInContext(readFileSync(new URL(name, STATIC), "utf8"), context, { filename: name });
    for (const event of BETWEEN) {
      for (const fn of listeners[event] || []) {
        fn({ target: any });
      }
    }
    for (const fn of frames.splice(0)) {
      try {
        fn(0);
      } catch (err) {
        reported.push(`a frame after ${name}: ${err}`);
      }
    }
  }
  assert.deepEqual(reported, []);
});
