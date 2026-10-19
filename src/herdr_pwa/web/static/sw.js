/* herdr-pwa service worker, served at /sw.js so its scope is the whole app.
 *
 * The one rule: this worker never answers with agent state. The screen IS live
 * agent state, so a cached document replayed later is a confident false claim
 * about who is blocked right now.
 *   - Navigations are fetched and returned, never stored; offline gets /offline,
 *     a page rendered from the build stamp alone, which cannot be handed a row.
 *   - Only an allow-list of static paths is ever answered from the cache.
 *   - Fragments, the feed, agent pages and every API route are not intercepted.
 *   - skipWaiting runs only when the page asks, never on install.
 */

"use strict";

/* The name moves only when PRECACHE does; activate sweeps every other name under the prefix. */
const CACHE_PREFIX = "herdr-shell-";
const CACHE_NAME = CACHE_PREFIX + "v5";
const OFFLINE_URL = "/offline";

const PRECACHE = [
  "/static/app.css",
  "/static/hp.js",
  "/static/drafts.js",
  "/static/backnav.js",
  "/static/sendhold.js",
  "/static/stream.js",
  "/static/geometry.js",
  "/static/interrupt.js",
  "/static/swipe.js",
  "/static/folds.js",
  "/static/chatend.js",
  "/static/voice.js",
  "/static/status.js",
  "/static/bubble.js",
  "/static/app.js",
  "/static/htmx.min.js",
  "/static/icon.svg",
  "/static/icon-192.png",
  "/static/icon-512.png",
  "/static/apple-touch-icon.png",
  "/manifest.webmanifest",
  OFFLINE_URL,
];

const PRECACHE_PATHS = new Set(PRECACHE);

/* These are unstamped URLs, so the install must not be satisfied from the HTTP cache. */
function installRequests() {
  return PRECACHE.map(function (url) {
    return new Request(url, { cache: "reload" });
  });
}

self.addEventListener("install", function (event) {
  /* addAll is all-or-nothing: a half-stocked cache would miss exactly when offline. */
  event.waitUntil(
    caches.open(CACHE_NAME).then(function (cache) {
      return cache.addAll(installRequests());
    }),
  );
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches
      .keys()
      .then(function (names) {
        return Promise.all(
          names.map(function (name) {
            if (name.startsWith(CACHE_PREFIX) && name !== CACHE_NAME) {
              return caches.delete(name);
            }
            return undefined;
          }),
        );
      })
      .then(function () {
        return self.clients.claim();
      }),
  );
});

/* The page sends this when the user taps "new build"; nothing else swaps the worker. */
self.addEventListener("message", function (event) {
  if (event.data && event.data.type === "hp-skip-waiting") {
    self.skipWaiting();
  }
});

function shellCache() {
  return caches.open(CACHE_NAME);
}

/* Network first: the cache is keyed without the stamp, so cache-first would pin the old build. */
function fromNetwork(event, request) {
  return fetch(request)
    .then(function (response) {
      /* 200 only: a 206 is "ok" and cache.put rejects it. */
      if (response && response.status === 200) {
        const copy = response.clone();
        const key = new URL(request.url);
        key.search = "";
        event.waitUntil(
          shellCache().then(function (cache) {
            return cache.put(key.href, copy);
          }),
        );
      }
      return response;
    })
    .catch(function () {
      /* This worker's cache only; caches.match would search every cache on the origin. */
      return shellCache()
        .then(function (cache) {
          return cache.match(request, { ignoreSearch: true });
        })
        .then(function (hit) {
          return hit || Response.error();
        });
    });
}

/* A document is returned untouched and never stored; offline gets the rowless shell. */
function navigation(request) {
  return fetch(request).catch(function () {
    return shellCache()
      .then(function (cache) {
        return cache.match(OFFLINE_URL);
      })
      .then(function (hit) {
        return hit || Response.error();
      });
  });
}

self.addEventListener("fetch", function (event) {
  const request = event.request;
  if (request.method !== "GET") {
    return;
  }
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) {
    return;
  }
  if (request.mode === "navigate") {
    event.respondWith(navigation(request));
    return;
  }
  /* An allow-list: anything not precached is left to the browser, unanswerable from here. */
  if (!PRECACHE_PATHS.has(url.pathname)) {
    return;
  }
  event.respondWith(fromNetwork(event, request));
});

/* Web Push. The payload is {v, event, agent, tag, title, body, quiet, ts}, already
 * sanitised by the server; its text goes to showNotification and nowhere else. */
const PAYLOAD_VERSION = 1;
const EVENT_BLOCKED = "blocked";
const EVENT_CLEAR = "clear";
const HERD_TAG = "herd";
const NOTIFY_ICON = "/static/icon-192.png";
const KEY_ROUTE = "/push/key";
const SUBSCRIBE_ROUTE = "/push/subscribe";
const UNSUBSCRIBE_ROUTE = "/push/unsubscribe";

/* The URL a tap opens is built from the payload, so its shape is checked, not trusted. */
const SESSION_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/* A pane: key (an agent with no session yet) has no page of its own here, so it opens the herd. */
function agentUrl(session) {
  const text = String(session || "");
  return SESSION_RE.test(text) ? "/agent/" + text : "/";
}

function readPayload(data) {
  if (!data) {
    return null;
  }
  try {
    const payload = data.json();
    return payload && payload.v === PAYLOAD_VERSION ? payload : null;
  } catch (err) {
    return null;
  }
}

function windowClients() {
  return self.clients.matchAll({ type: "window", includeUncontrolled: true });
}

/* Only the browser knows the page is on screen; it is shown silently, never dropped. */
function onScreenFor(open, session) {
  const wanted = agentUrl(session);
  return open.some(function (client) {
    return (
      client.visibilityState === "visible" &&
      new URL(client.url).pathname === wanted
    );
  });
}

function closeTag(tag) {
  return self.registration.getNotifications({ tag: tag }).then(function (open) {
    open.forEach(function (notification) {
      notification.close();
    });
    return open.length;
  });
}

/* A push that shows nothing makes the browser show its own generic message instead. */
function showUnreadable() {
  return self.registration.showNotification("herdr", {
    body: "A notification this app is too old to read. Open the app to see the herd.",
    tag: "hp-unreadable",
    icon: NOTIFY_ICON,
  });
}

function onPush(data) {
  const payload = readPayload(data);
  if (!payload) {
    return showUnreadable();
  }
  if (payload.event === EVENT_CLEAR) {
    return closeTag(payload.tag || HERD_TAG);
  }
  return windowClients().then(function (open) {
    const onScreen = onScreenFor(open, payload.agent);
    /* The server says quiet when the slot shrank; the browser says quiet when it is on screen. */
    const quiet = onScreen || payload.quiet === true;
    return self.registration.showNotification(payload.title || "herdr", {
      body: payload.body || "",
      tag: payload.tag || HERD_TAG,
      icon: NOTIFY_ICON,
      renotify: !quiet,
      silent: quiet,
      /* A question nobody has answered stays; news may fade. */
      requireInteraction: payload.event === EVENT_BLOCKED && !onScreen,
      data: {
        session: payload.agent,
        event: payload.event,
        url: agentUrl(payload.agent),
      },
    });
  });
}

self.addEventListener("push", function (event) {
  event.waitUntil(onPush(event.data));
});

/* Reuse the open window: two copies of a live-state app are two truths. */
function openAgent(url) {
  const target = new URL(url, self.location.origin).href;
  return windowClients().then(function (open) {
    const ours = open.filter(function (client) {
      return new URL(client.url).origin === self.location.origin;
    });
    const already = ours.filter(function (client) {
      return client.url === target;
    })[0];
    if (already) {
      return already.focus();
    }
    return navigateOne(ours, target);
  });
}

function navigateOne(ours, target) {
  const client = ours[0];
  if (!client || typeof client.navigate !== "function") {
    return self.clients.openWindow(target);
  }
  /* navigate rejects on an uncontrolled client; focusing it would land on the wrong page. */
  return client.navigate(target).then(
    function (moved) {
      return (moved || client).focus();
    },
    function () {
      return self.clients.openWindow(target);
    },
  );
}

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  const data = event.notification.data || {};
  event.waitUntil(openAgent(typeof data.url === "string" ? data.url : "/"));
});

function postJson(url, body) {
  return fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

/* applicationServerKey takes bytes everywhere and a string only in some browsers. */
function keyBytes(base64url) {
  const padded = String(base64url).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(padded + "=".repeat((4 - (padded.length % 4)) % 4));
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i += 1) {
    bytes[i] = raw.charCodeAt(i);
  }
  return bytes;
}

/* A rotated subscription nobody re-registers is a phone that silently stops buzzing. */
function resubscribe(event) {
  const previous = event.oldSubscription;
  const supplied = event.newSubscription;
  const next = supplied
    ? Promise.resolve(supplied)
    : fetch(KEY_ROUTE, { credentials: "same-origin" })
        .then(function (response) {
          return response.ok ? response.json() : Promise.reject(response.status);
        })
        .then(function (body) {
          return self.registration.pushManager.subscribe({
            userVisibleOnly: true,
            applicationServerKey: keyBytes(body.publicKey),
          });
        });
  return next
    .then(function (subscription) {
      return postJson(SUBSCRIBE_ROUTE, subscription).then(function () {
        if (previous && previous.endpoint !== subscription.endpoint) {
          return postJson(UNSUBSCRIBE_ROUTE, { endpoint: previous.endpoint });
        }
        return undefined;
      });
    })
    .catch(function () {
      /* Nowhere to report from here; the alerts page reads the device count on its next load. */
    });
}

self.addEventListener("pushsubscriptionchange", function (event) {
  event.waitUntil(resubscribe(event));
});
