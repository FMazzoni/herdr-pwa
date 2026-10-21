/* Web Push on the page, in app.js: what it asks the box on load, the one subscribe a tap sends and
 * the endpoint it swaps out, and when the "alerts are off" banner shows. Run against a stand-in
 * page, push service and box, so no real push is ever subscribed or sent. */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { runInNewContext } from "node:vm";

const STATIC = new URL("../../src/herdr_pwa/web/static/", import.meta.url);
const OLD = "https://web.push.apple.com/old";
const STALE = "https://web.push.apple.com/stale";

function element() {
  const listeners = {};
  return {
    hidden: true,
    textContent: "",
    addEventListener: (name, fn) => {
      (listeners[name] = listeners[name] || []).push(fn);
    },
    fire: (name) => (listeners[name] || []).forEach((fn) => fn({ currentTarget: null })),
  };
}

function memory(entries = {}) {
  const kept = new Map(Object.entries(entries));
  return {
    kept,
    getItem: (key) => (kept.has(key) ? kept.get(key) : null),
    setItem: (key, value) => kept.set(key, value),
  };
}

// The push service: at most one subscription, and each new one gets a new endpoint.
function service(endpoint) {
  let count = 0;
  const made = (url) => ({
    endpoint: url,
    unsubscribed: false,
    toJSON: () => ({ endpoint: url, keys: { p256dh: "p", auth: "a" } }),
    unsubscribe() {
      this.unsubscribed = true;
      manager.current = null;
      return Promise.resolve(true);
    },
  });
  const manager = {
    current: endpoint ? made(endpoint) : null,
    getSubscription: () => Promise.resolve(manager.current),
    subscribe: () => {
      count += 1;
      manager.current = made(`https://web.push.apple.com/new-${count}`);
      return Promise.resolve(manager.current);
    },
  };
  return manager;
}

// The box: the endpoints it holds, and every request the page sent it.
function box(held) {
  const store = new Set(held);
  const sent = [];
  const answer = (data) => Promise.resolve({ ok: true, json: () => Promise.resolve(data) });
  const fetch = (url, init) => {
    const body = init && init.body ? JSON.parse(init.body) : null;
    sent.push([url, body]);
    if (url === "/push/key") {
      return answer({ enabled: true, publicKey: "AAAA" });
    }
    if (url === "/push/device") {
      return answer({ known: store.has(body.endpoint), devices: store.size, armed: true });
    }
    if (url === "/push/subscribe") {
      if (body.replaces) {
        store.delete(body.replaces);
      }
      store.add(body.endpoint);
      return answer({ endpoint: body.endpoint, devices: store.size, armed: true });
    }
    if (url === "/push/unsubscribe") {
      return answer({ removed: store.delete(body.endpoint), devices: store.size });
    }
    throw new Error(`unexpected ${url}`);
  };
  return { store, sent, fetch };
}

function load({ panel = false, standalone = true, permission = "granted", endpoint = null, held = [], storage = memory() }) {
  const ids = {};
  for (const id of ["push-on", "push-off", "push-state", "push-devices", "hp-alerts-off", "hp-alerts-off-what", "hp-alerts-on"]) {
    ids[id] = element();
  }
  const documentListeners = {};
  const document = {
    hidden: false,
    addEventListener: (name, fn) => {
      (documentListeners[name] = documentListeners[name] || []).push(fn);
    },
    getElementById: (id) => ids[id] || null,
    querySelector: (selector) => (selector === "[data-hp-push]" && panel ? element() : null),
    querySelectorAll: () => [],
  };
  const manager = service(endpoint);
  const server = box(held);
  const notification = {
    permission,
    requestPermission: () => Promise.resolve((notification.permission = "granted")),
  };
  const window = {
    document,
    navigator: {
      standalone,
      serviceWorker: {
        register: () => Promise.resolve(),
        addEventListener: () => {},
        ready: Promise.resolve({ pushManager: manager }),
      },
    },
    localStorage: storage,
    PushManager: function PushManager() {},
    Notification: notification,
    fetch: server.fetch,
    atob: (text) => Buffer.from(text, "base64").toString("binary"),
    addEventListener: () => {},
  };
  window.window = window;
  for (const name of ["hp.js", "app.js"]) {
    runInNewContext(readFileSync(new URL(name, STATIC), "utf8"), window);
  }
  const resume = () => (documentListeners.visibilitychange || []).forEach((fn) => fn({}));
  return { ids, manager, server, notification, storage, resume };
}

// Every promise the page chained, run out.
async function settle() {
  for (let i = 0; i < 30; i += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

const subscribes = (server) => server.sent.filter(([url]) => url === "/push/subscribe");

test("opening the alerts page asks about this endpoint only and subscribes nothing", async () => {
  const page = load({ panel: true, endpoint: OLD, held: [OLD, "https://web.push.apple.com/other"] });
  await settle();
  assert.deepEqual(page.server.sent, [["/push/device", { endpoint: OLD }]]);
  assert.equal(page.ids["push-state"].textContent, "Alerts are on for this device.");
  assert.equal(page.ids["push-devices"].textContent, "2 devices subscribed on this box.");
  assert.equal(page.ids["push-off"].hidden, false);
  assert.equal(page.storage.getItem("hp-push-endpoint"), OLD);
});

test("turning alerts on sends one subscribe, swapping out the endpoint registered before", async () => {
  const storage = memory({ "hp-push-endpoint": OLD, "hp-alerts-declined": "1" });
  const page = load({ panel: true, permission: "default", held: [OLD], storage });
  await settle();
  assert.equal(page.ids["push-state"].textContent, "Alerts are off on this device.");
  page.ids["push-on"].fire("click");
  await settle();
  const posts = subscribes(page.server);
  assert.equal(posts.length, 1);
  assert.equal(posts[0][1].replaces, OLD);
  assert.deepEqual([...page.server.store], ["https://web.push.apple.com/new-1"]);
  assert.equal(storage.getItem("hp-push-endpoint"), "https://web.push.apple.com/new-1");
  assert.equal(storage.getItem("hp-alerts-declined"), "0");
  assert.equal(page.ids["push-devices"].textContent, "1 device subscribed on this box.");
  assert.equal(page.ids["push-state"].textContent, "Alerts are on for this device.");
});

test("an endpoint the box lost shows the banner, and its button subscribes afresh once", async () => {
  const page = load({ endpoint: STALE, held: ["https://web.push.apple.com/other"] });
  await settle();
  assert.equal(page.ids["hp-alerts-off"].hidden, false);
  assert.equal(page.ids["hp-alerts-on"].hidden, false);
  const stale = page.manager.current;
  page.ids["hp-alerts-on"].fire("click");
  await settle();
  assert.equal(stale.unsubscribed, true);
  assert.equal(subscribes(page.server).length, 1);
  assert.equal(page.ids["hp-alerts-off"].hidden, true);
});

test("permission not yet granted shows the banner on a Home Screen app", async () => {
  const page = load({ permission: "default" });
  await settle();
  assert.equal(page.ids["hp-alerts-off"].hidden, false);
  assert.deepEqual(page.server.sent, []);
});

test("permission refused says where to allow it, with no button that cannot work", async () => {
  const page = load({ permission: "denied" });
  await settle();
  assert.equal(page.ids["hp-alerts-off"].hidden, false);
  assert.equal(page.ids["hp-alerts-on"].hidden, true);
  assert.match(page.ids["hp-alerts-off-what"].textContent, /Settings/);
});

test("no banner in a browser tab, on a device whose alerts are on, or after choosing off", async () => {
  const tab = load({ standalone: false, permission: "default" });
  const on = load({ endpoint: OLD, held: [OLD] });
  const declined = load({ permission: "default", storage: memory({ "hp-alerts-declined": "1" }) });
  await settle();
  assert.deepEqual(tab.server.sent, []);
  for (const page of [tab, on, declined]) {
    assert.equal(page.ids["hp-alerts-off"].hidden, true);
  }
});

test("coming back from Settings checks again", async () => {
  const page = load({ permission: "default" });
  await settle();
  assert.equal(page.ids["hp-alerts-off"].hidden, false);
  page.notification.permission = "granted";
  page.manager.current = { endpoint: OLD };
  page.server.store.add(OLD);
  page.resume();
  await settle();
  assert.equal(page.ids["hp-alerts-off"].hidden, true);
});

test("storage whose every call throws still subscribes, with nothing to replace", async () => {
  const refuse = () => {
    throw new Error("SecurityError");
  };
  const page = load({ panel: true, permission: "default", storage: { getItem: refuse, setItem: refuse } });
  await settle();
  page.ids["push-on"].fire("click");
  await settle();
  const posts = subscribes(page.server);
  assert.equal(posts.length, 1);
  assert.equal("replaces" in posts[0][1], false);
  assert.equal(page.ids["push-state"].textContent, "Alerts are on for this device.");
});
