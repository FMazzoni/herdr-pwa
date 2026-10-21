/* A service-worker environment small enough to reason about, so sw.js can be
 * asked what it DOES rather than grepped for what it says.
 *
 * Every previous test of that file was a substring assertion, and a mutation
 * that made the worker cache /feed — the one thing its header promises never to
 * do — left the entire Python suite green. Behaviour needs a runtime, and the
 * only one available without npm is the node already on this box.
 *
 * Usage:  node harness.mjs <path-to-sw.js>
 * Prints one JSON object of observations to stdout. It asserts nothing; the
 * Python test owns the expectations, so a new question does not mean editing
 * JavaScript.
 */
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";

const swPath = process.argv[2];
const ORIGIN = "https://box.tailnet.ts.net";

/* 65 raw bytes, leading 0x04, base64url unpadded -- the only shape a browser
 * accepts as an applicationServerKey. */
const VAPID_PUBLIC_KEY = "BAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4fICEiIyQlJicoKSorLC0uLzAxMjM0NTY3ODk6Ozw9Pj8";

class FakeCache {
  constructor(name) { this.name = name; this.entries = new Map(); }
  async addAll(requests) {
    for (const r of requests) {
      const res = await fakeFetch(r);
      if (!res.ok) throw new TypeError(`addAll failed for ${r.url || r} (${res.status})`);
      this.entries.set(new URL(r.url || r, ORIGIN).href, res);
    }
  }
  async put(key, res) {
    /* The real cache API rejects a partial response, and sw.js is expected to
     * never hand one over. Refusing it here is what makes that observable. */
    if (res && res.status === 206) throw new TypeError("cache.put on a 206");
    this.entries.set(new URL(key.url || key, ORIGIN).href, res);
  }
  async match(req, opts = {}) {
    const url = new URL(typeof req === "string" ? req : req.url, ORIGIN);
    if (opts.ignoreSearch) url.search = "";
    return this.entries.get(url.href) || undefined;
  }
}

const caches = {
  store: new Map(),
  async open(name) {
    if (!this.store.has(name)) this.store.set(name, new FakeCache(name));
    return this.store.get(name);
  },
  async keys() { return [...this.store.keys()]; },
  async delete(name) { return this.store.delete(name); },
  async match(req, opts) {
    for (const cache of this.store.values()) {
      const hit = await cache.match(req, opts);
      if (hit) return hit;
    }
    return undefined;
  },
};

/* The network. `missing` makes a URL 404 (to probe addAll's all-or-nothing
 * claim); `partial` makes it answer 206, which is `ok` but uncacheable;
 * `offline` makes every fetch reject, which is the state the cache exists
 * for. */
const net = { offline: false, missing: new Set(), partial: new Set(), log: [], modes: [] };
async function fakeFetch(input) {
  const url = new URL(typeof input === "string" ? input : input.url, ORIGIN);
  net.log.push(url.pathname + url.search);
  net.modes.push({ path: url.pathname, cache: (input && input.cache) || "default" });
  if (net.offline) throw new TypeError("NetworkError");
  const missing = net.missing.has(url.pathname);
  const partial = net.partial.has(url.pathname);
  return {
    ok: !missing,
    status: missing ? 404 : partial ? 206 : 200,
    url: url.href,
    body: `body-of:${url.pathname}${url.search}`,
    /* Only /push/key has a body sw.js reads; everything else answers an
     * empty object so a POST's `.json()` resolves rather than throwing. */
    async json() {
      return url.pathname === "/push/key"
        ? { enabled: true, publicKey: VAPID_PUBLIC_KEY }
        : {};
    },
    clone() { return { ...this, clone: this.clone }; },
  };
}

/* A Request that carries the two fields sw.js reads: `mode`, which is how a
 * navigation is told from a subresource, and `cache`, which is what
 * `{cache: "reload"}` sets on the install fetches. */
class FakeRequest {
  constructor(input, init = {}) {
    this.url = new URL(typeof input === "string" ? input : input.url, ORIGIN).href;
    this.method = init.method || (input && input.method) || "GET";
    this.mode = init.mode || (input && input.mode) || "cors";
    this.cache = init.cache || "default";
  }
}

/* A notification, as much of one as sw.js touches: it is shown, it may be
 * looked up by tag, and it can be closed. `shown` is the whole record, so
 * "was anything shown at all" is answerable — which is the question a push
 * that must never be silent-and-empty turns on. */
const notifications = { shown: [], live: [] };

/* The windows the app has open. `visibilityState` is what sw.js reads to decide
 * whether somebody is already looking at the agent being notified about. */
const windows = { open: [], opened: [], focused: [], navigated: [] };

function makeClient(url, visibilityState = "hidden", navigable = true) {
  const client = {
    url: new URL(url, ORIGIN).href,
    visibilityState,
    focus: async () => { windows.focused.push(client.url); return client; },
  };
  if (navigable) {
    client.navigate = async (target) => {
      windows.navigated.push(target);
      client.url = new URL(target, ORIGIN).href;
      return client;
    };
  }
  return client;
}

const listeners = new Map();
const self = {
  location: new URL(ORIGIN + "/sw.js"),
  clients: {
    claim: async () => "claimed",
    matchAll: async () => windows.open,
    openWindow: async (url) => { windows.opened.push(url); return makeClient(url); },
  },
  addEventListener: (type, fn) => listeners.set(type, fn),
  skipWaiting: () => { observations.calledSkipWaiting = true; },
  registration: {
    showNotification: async (title, options = {}) => {
      const notification = { title, ...options, close() { notifications.live = notifications.live.filter((n) => n !== notification); } };
      notifications.shown.push(notification);
      /* THE PLATFORM RULE THE HERD SLOT RESTS ON: a notification with a tag REPLACES
       * the live one carrying the same tag rather than stacking beside it. A
       * fake that appends unconditionally would make "one slot for the herd"
       * untestable here, which is the same class of hole this whole file
       * exists to close. */
      if (options.tag) {
        notifications.live = notifications.live.filter((n) => n.tag !== options.tag);
      }
      notifications.live.push(notification);
      return notification;
    },
    getNotifications: async ({ tag } = {}) =>
      notifications.live.filter((n) => tag === undefined || n.tag === tag),
    pushManager: {
      subscribe: async (options) => {
        observations.subscribeOptions = {
          userVisibleOnly: options.userVisibleOnly,
          keyBytes: Array.from(options.applicationServerKey || []),
        };
        return { endpoint: ORIGIN + "/push/new", toJSON() { return { endpoint: this.endpoint }; } };
      },
    },
  },
};

const observations = { calledSkipWaiting: false, subscribeOptions: null };

const sandbox = {
  self, caches, fetch: fakeFetch, URL, Request: FakeRequest,
  Response: { error: () => ({ type: "error", ok: false }) },
  Promise, Set, Map, console, TypeError, Uint8Array, JSON, atob, Error,
};
runInNewContext(readFileSync(swPath, "utf8"), sandbox);

function makeEvent(request) {
  const ev = { request, responded: false, response: undefined, waits: [] };
  ev.respondWith = (p) => { ev.responded = true; ev.response = p; };
  ev.waitUntil = (p) => ev.waits.push(p);
  return ev;
}
async function dispatch(type, ev) {
  const fn = listeners.get(type);
  if (!fn) throw new Error(`no ${type} listener registered`);
  fn(ev);
  if (ev.responded) await ev.response;
  await Promise.all(ev.waits || []);
  return ev;
}
const req = (path, method = "GET") => new FakeRequest(path, { method });
const nav = (path) => new FakeRequest(path, { mode: "navigate" });
const shell = async () => (await caches.open((await caches.keys())[0])).entries;

/* --- install ------------------------------------------------------------ */
await dispatch("install", makeEvent(null));
const cacheNames = await caches.keys();
const precached = [...(await caches.open(cacheNames[0])).entries.keys()];
/* Every install fetch must bypass the HTTP cache, not merely most of them. */
const installCacheModes = [...new Set(net.modes.map((m) => m.cache))];

/* --- which requests does the worker take responsibility for? ------------ */
const probes = {
  feed: req("/feed"),
  chat: req("/agent/abc-123/chat"),
  pane: req("/agent/abc-123/pane"),
  pushKey: req("/push/key"),
  document: nav("/"),
  agentPage: nav("/agent/abc-123"),
  stampedAsset: req("/static/app.css?v=2.1"),
  unstampedAsset: req("/static/app.css"),
  manifest: req("/manifest.webmanifest"),
  crossOrigin: new FakeRequest("https://evil.example/x.css"),
  postToFeed: req("/feed", "POST"),
  fileWatch: req("/files/0123456789ab/watch?path=a.py&v=1"),
  fileImage: req("/files/0123456789ab/raw?path=logo.png"),
  fileFind: req("/files/0123456789ab/find?q=a"),
};
const intercepted = {};
for (const [name, request] of Object.entries(probes)) {
  const ev = await dispatch("fetch", makeEvent(request));
  intercepted[name] = ev.responded;
}

/* --- a navigation is passed through and NEVER stored -------------------- */
const onlineNav = await dispatch("fetch", makeEvent(nav("/")));
const onlineNavBody = (await onlineNav.response).body;
/* A file is the repository's contents: opened, it passes through and is never kept either. */
const fileNav = await dispatch("fetch", makeEvent(nav("/files/0123456789ab/file?path=a.py")));
const fileNavBody = (await fileNav.response).body;
const cachedDocumentKeys = [...(await shell()).keys()].filter((k) => {
  const path = new URL(k).pathname;
  return path === "/" || path.startsWith("/agent/") || path.startsWith("/files");
});

/* --- the cache.put is inside waitUntil, not fire-and-forget ------------- */
const assetEvent = await dispatch("fetch", makeEvent(req("/static/app.css?v=1")));
const cachePutIsAwaited = assetEvent.waits.length > 0;

/* --- network-first, not cache-first ------------------------------------- */
/* The cache now holds build 1. A cache-first worker hands back build 1's bytes
 * under build 2's URL. */
const onlineSecond = await (await dispatch("fetch", makeEvent(req("/static/app.css?v=2")))).response;

/* --- a 206 must not be handed to cache.put ------------------------------ */
/* Drop the precached 200 first, so "is there an entry afterwards" answers about
 * the partial response and not about the install. */
(await shell()).delete(new URL("/static/icon.svg", ORIGIN).href);
net.partial.add("/static/icon.svg");
let partialThrew = false;
try {
  await dispatch("fetch", makeEvent(req("/static/icon.svg?v=9")));
} catch { partialThrew = true; }
net.partial.clear();
const partialCached = (await shell()).has(new URL("/static/icon.svg", ORIGIN).href);
/* Put the shell back the way the install left it. */
await dispatch("fetch", makeEvent(req("/static/icon.svg?v=9")));

/* --- and the cache does earn its keep when the network is gone ---------- */
net.offline = true;
const offlineHit = await (await dispatch("fetch", makeEvent(req("/static/app.css?v=2")))).response;
const offlineNav = await (await dispatch("fetch", makeEvent(nav("/")))).response;
const offlineAgentNav = await (await dispatch("fetch", makeEvent(nav("/agent/abc-123")))).response;
const offlineFeed = await dispatch("fetch", makeEvent(req("/feed")));
net.offline = false;

/* --- the fallback comes from THIS cache, not any cache on the origin ---- */
/* A decoy cache outside the prefix holding a different app.css. The worker must
 * not find it: `caches.match()` searches every cache in the origin. */
const decoy = await caches.open("someone-elses-cache");
await decoy.put("/static/app.css", { ok: true, status: 200, body: "body-of:DECOY" });
const mine = await caches.open(cacheNames[0]);
mine.entries.delete(new URL("/static/app.css", ORIGIN).href);
net.offline = true;
const decoyProbe = await (await dispatch("fetch", makeEvent(req("/static/app.css?v=2")))).response;
net.offline = false;
caches.store.delete("someone-elses-cache");

/* --- the update swap is the page's decision, never the worker's --------- */
const skipWaitingBeforeMessage = observations.calledSkipWaiting;
await dispatch("message", { data: { type: "something-else" }, waits: [] });
const skipWaitingOnAnyMessage = observations.calledSkipWaiting;
await dispatch("message", { data: { type: "hp-skip-waiting" }, waits: [] });
const skipWaitingOnRequest = observations.calledSkipWaiting;

/* --- push: what a notification says, and when it stays quiet ------------- */
const SESSION = "5e550001-0000-0000-0000-000000000000";
const pushEvent = (payload) => ({
  data: payload === null ? null : { json: () => { if (typeof payload === "string") throw new SyntaxError("bad"); return payload; } },
  waits: [],
  waitUntil(p) { this.waits.push(p); },
});
const notify = (event, extra = {}) => ({
  v: 1, event, agent: SESSION, tag: "herd",
  title: "needs you: fix the parser", body: "herdr - a question",
  quiet: false, ts: 1700000000, ...extra,
});

async function push(payload) {
  notifications.shown.length = 0;
  await dispatch("push", pushEvent(payload));
  return notifications.shown[notifications.shown.length - 1];
}

const blockedNotification = await push(notify("blocked"));
const doneNotification = await push(notify("done"));

/* Nobody is looking: it must alert. */
const alertsWhenNobodyIsWatching = {
  silent: !!blockedNotification.silent,
  renotify: !!blockedNotification.renotify,
  requireInteraction: !!blockedNotification.requireInteraction,
};

/* The app is open ON THAT AGENT and visible: it must not buzz, and must still
 * show something, because a push that shows nothing is a push the browser may
 * answer with a generic message of its own. */
windows.open = [makeClient("/agent/" + SESSION, "visible")];
const onScreenNotification = await push(notify("blocked"));
/* Open, but on the herd rather than on that agent: still an alert. */
windows.open = [makeClient("/", "visible")];
const elsewhereNotification = await push(notify("blocked"));
/* Open on that agent but in the background: still an alert. */
windows.open = [makeClient("/agent/" + SESSION, "hidden")];
const backgroundNotification = await push(notify("blocked"));
windows.open = [];

/* The server says "this is a retraction, re-render without buzzing".
 * It is a SEPARATE reason from the client's own, so it must work with nobody
 * looking at the app -- and it must not un-stick a question nobody answered. */
const quietUpdate = await push(notify("blocked", { quiet: true, title: "2 agents need you" }));

/* A summary about the whole herd carries no session, so the tap lands
 * on the feed rather than on a made-up agent page. */
const herdSummary = await push(notify("blocked", { agent: "", title: "3 agents need you" }));

/* A payload from a newer server is not guessed at. */
const unreadableShown = (await push("not json")) || null;
const wrongVersionShown = (await push(notify("blocked", { v: 99 }))) || null;
const noDataShown = (await push(null)) || null;

/* The clear takes the notification down and shows nothing. */
notifications.shown.length = 0;
notifications.live.length = 0;
await dispatch("push", pushEvent(notify("blocked")));
const liveBeforeClear = notifications.live.length;
await dispatch("push", pushEvent({ v: 1, event: "clear", agent: SESSION, tag: "herd", ts: 1 }));
const liveAfterClear = notifications.live.length;
const shownByClear = notifications.shown.length - 1;

/* three transitions in a row land on ONE slot, not three stacked
 * notifications. `tag` is what the platform replaces on, so the count of LIVE
 * notifications is the assertion -- `shown` counts renders, which is three. */
notifications.shown.length = 0;
notifications.live.length = 0;
await dispatch("push", pushEvent(notify("blocked", { title: "1 agent needs you" })));
await dispatch("push", pushEvent(notify("blocked", { title: "2 agents need you" })));
await dispatch("push", pushEvent(notify("blocked", { title: "3 agents need you" })));
const liveAfterThree = notifications.live.length;
const rendersOfThree = notifications.shown.length;

/* --- the tap ------------------------------------------------------------- */
const click = (data, url = "/agent/" + SESSION) => ({
  notification: { data, tag: "t", close() { this.closed = true; }, closed: false },
  waits: [],
  waitUntil(p) { this.waits.push(p); },
});

windows.open = [];
let ev = click({ url: "/agent/" + SESSION });
await dispatch("notificationclick", ev);
const openedWithNoWindow = windows.opened.slice();
const closedOnClick = ev.notification.closed;

windows.opened.length = 0;
windows.navigated.length = 0;
windows.focused.length = 0;
windows.open = [makeClient("/")];
await dispatch("notificationclick", click({ url: "/agent/" + SESSION }));
const navigatedExisting = windows.navigated.slice();
const openedAsWell = windows.opened.slice();

/* An uncontrolled window cannot be navigated; a new one is opened rather than
 * focusing the wrong page. */
windows.opened.length = 0;
windows.navigated.length = 0;
windows.open = [makeClient("/", "hidden", false)];
await dispatch("notificationclick", click({ url: "/agent/" + SESSION }));
const openedWhenNotNavigable = windows.opened.slice();

/* A payload whose `agent` is not a session UUID gets the herd, never a URL
 * built out of whatever the string was. */
notifications.shown.length = 0;
windows.open = [];
await dispatch("push", pushEvent(notify("blocked", { agent: "../../evil" })));
const hostileUrl = notifications.shown[0].data.url;

/* An agent with no session is filed under its ADDRESS; v2 has no page for it yet, so the tap
 * goes to the herd. Still a checked shape, not a passthrough. */
notifications.shown.length = 0;
windows.open = [];
await dispatch("push", pushEvent(notify("blocked", { agent: "pane:w1:p99" })));
const paneUrl = notifications.shown[0].data.url;

notifications.shown.length = 0;
await dispatch("push", pushEvent(notify("blocked", { agent: "pane:../../evil" })));
const hostilePaneUrl = notifications.shown[0].data.url;

/* opencode and hermes name their sessions in their own shapes; each still opens its agent,
 * and its alert is quiet while that agent is on screen. */
const OPENCODE = "ses_3fd62ba5bffemIxWVhRda5wgS7";
const HERMES = "20260101_120000_aaaaaa";
notifications.shown.length = 0;
await dispatch("push", pushEvent(notify("blocked", { agent: OPENCODE })));
const opencodeUrl = notifications.shown[0].data.url;
notifications.shown.length = 0;
await dispatch("push", pushEvent(notify("blocked", { agent: HERMES })));
const hermesUrl = notifications.shown[0].data.url;
windows.open = [makeClient("/agent/" + OPENCODE, "visible")];
const opencodeOnScreen = await push(notify("blocked", { agent: OPENCODE }));
const opencodeOnScreenSilent = !!opencodeOnScreen.silent;
windows.open = [makeClient("/", "visible")];
const opencodeFromHerd = await push(notify("blocked", { agent: OPENCODE }));
const opencodeFromHerdSilent = !!opencodeFromHerd.silent;
windows.open = [];

/* --- the subscription the browser rotated under us ----------------------- */
windows.open = [];
net.log.length = 0;
await dispatch("pushsubscriptionchange", {
  oldSubscription: { endpoint: ORIGIN + "/push/old" },
  newSubscription: null,
  waits: [],
  waitUntil(p) { this.waits.push(p); },
});
const resubscribePosts = net.log.slice();

/* --- all-or-nothing install --------------------------------------------- */
caches.store.clear();
net.missing.add("/static/icon-512.png");
let installFailed = false;
try { await dispatch("install", makeEvent(null)); } catch { installFailed = true; }
net.missing.clear();

console.log(JSON.stringify({
  precached,
  installCacheModes,
  intercepted,
  fileNavBody,
  onlineNavBody,
  cachedDocumentKeys,
  cachePutIsAwaited,
  partialThrew,
  partialCached,
  onlineSecondBody: onlineSecond && onlineSecond.body,
  offlineHitBody: offlineHit && offlineHit.body,
  offlineNavBody: offlineNav && offlineNav.body,
  offlineAgentNavBody: offlineAgentNav && offlineAgentNav.body,
  offlineFeedResponded: offlineFeed.responded,
  decoyProbeBody: decoyProbe && decoyProbe.body,
  decoyProbeType: decoyProbe && decoyProbe.type,
  skipWaitingBeforeMessage,
  skipWaitingOnAnyMessage,
  skipWaitingOnRequest,
  installFailsWhenAnAssetIsMissing: installFailed,
  blockedNotification: {
    title: blockedNotification.title, body: blockedNotification.body,
    tag: blockedNotification.tag, icon: blockedNotification.icon,
    data: blockedNotification.data,
  },
  doneRequiresInteraction: !!doneNotification.requireInteraction,
  alertsWhenNobodyIsWatching,
  onScreen: { shown: !!onScreenNotification, silent: !!onScreenNotification.silent, renotify: !!onScreenNotification.renotify },
  elsewhereSilent: !!elsewhereNotification.silent,
  backgroundSilent: !!backgroundNotification.silent,
  unreadableTitle: unreadableShown && unreadableShown.title,
  unreadableBody: unreadableShown && unreadableShown.body,
  wrongVersionTitle: wrongVersionShown && wrongVersionShown.title,
  noDataTitle: noDataShown && noDataShown.title,
  liveBeforeClear, liveAfterClear, shownByClear,
  quietUpdate: {
    silent: !!quietUpdate.silent, renotify: !!quietUpdate.renotify,
    requireInteraction: !!quietUpdate.requireInteraction, title: quietUpdate.title,
  },
  herdSummaryUrl: herdSummary.data && herdSummary.data.url,
  herdSummaryTag: herdSummary.tag,
  liveAfterThree, rendersOfThree,
  openedWithNoWindow, closedOnClick,
  navigatedExisting, openedAsWell, openedWhenNotNavigable,
  hostileUrl,
  paneUrl,
  hostilePaneUrl,
  opencodeUrl,
  hermesUrl,
  opencodeOnScreenSilent,
  opencodeFromHerdSilent,
  resubscribePosts,
  subscribeOptions: observations.subscribeOptions,
}, null, 2));
