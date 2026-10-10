/* herdr-pwa: the only hand-written client script. It never builds markup from
 * strings and never evals; htmx trigger filters would need eval, so the server
 * stops a hidden page's polls (HTTP 286) and this script re-arms them. */
(function () {
  "use strict";

  /* Every request from a hidden page says so; its poll answers 286 and stops. */
  document.addEventListener("htmx:configRequest", function (event) {
    if (document.hidden) {
      event.detail.headers["HP-Hidden"] = "1";
    }
  });

  /* htmx 2 refuses to swap a non-2xx; a fragment the server marked is the honest
   * failure screen, and it carries the poll that heals the page. */
  document.addEventListener("htmx:beforeSwap", function (event) {
    var xhr = event.detail.xhr;
    if (xhr && xhr.status >= 400 && xhr.getResponseHeader("HP-Fragment") === "1") {
      event.detail.shouldSwap = true;
      event.detail.isError = false;
    }
    sheetAnswer(event.detail);
  });

  /* iOS Safari applies :active only under a touch listener; the press states in app.css need one. */
  document.addEventListener("touchstart", function () {}, { passive: true });

  /* ---- Back belongs to the herd ----
   * The feed is the root: only its own links push. Every other move replaces the
   * entry it leaves, so a swipe back from anywhere lands on the feed, never on a
   * tab, a spent form or a door that forwards onward. */

  function isSheet(detail) {
    var elt = detail.requestConfig && detail.requestConfig.elt;
    return !!(elt && elt.closest && elt.closest("[data-hp-sheet]"));
  }

  /* A sheet's refusal is a whole page at a 4xx and is the answer, so it is shown;
   * a failure fragment has no main to select, so it replaces the main whole. */
  function sheetAnswer(detail) {
    var xhr = detail.xhr;
    if (!xhr || !isSheet(detail) || xhr.status < 400) {
      return;
    }
    detail.shouldSwap = true;
    detail.isError = false;
    if (String(xhr.responseText).indexOf("<main") === -1) {
      detail.selectOverride = "unset";
    }
  }

  function plainClick(event) {
    return event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey;
  }

  document.addEventListener("click", function (event) {
    var link = event.target && event.target.closest ? event.target.closest("a[data-hp-replace]") : null;
    if (!link || !plainClick(event)) {
      return;
    }
    event.preventDefault();
    /* Leaving mid-write would hide whether it landed. */
    if (!document.body.classList.contains("is-sending")) {
      window.location.replace(link.href);
    }
  });

  /* A finished write names its page; htmx's own redirect would push, so the page replaces. */
  document.addEventListener("hp:opened", function (event) {
    var where = event.detail && event.detail.where;
    if (typeof where === "string" && where.charAt(0) === "/" && where.charAt(1) !== "/") {
      window.location.replace(where);
    }
  });

  /* Re-fetch every polling region once the page is visible again. */
  function rearm() {
    if (document.hidden || typeof window.htmx === "undefined") {
      return;
    }
    var nodes = document.querySelectorAll("[data-hp-poll]");
    for (var i = 0; i < nodes.length; i++) {
      window.htmx.ajax("GET", nodes[i].getAttribute("data-hp-poll"), {
        target: nodes[i],
        swap: "outerHTML",
      });
    }
  }

  /* A composer's draft survives navigation: kept per session in this tab only,
   * restored as a value (never markup), dropped once sent or at a no-echo prompt. */
  var DRAFT = "hp-draft:";

  function store() {
    try {
      return window.sessionStorage;
    } catch (err) {
      return null;
    }
  }

  function keep(box) {
    var s = store();
    if (!s) {
      return;
    }
    try {
      if (box.value) {
        s.setItem(DRAFT + box.getAttribute("data-hp-draft"), box.value);
      } else {
        s.removeItem(DRAFT + box.getAttribute("data-hp-draft"));
      }
    } catch (err) {
      /* storage full or blocked: the draft lives only in the box. */
    }
  }

  function settle(root) {
    var boxes = root.querySelectorAll("textarea[data-hp-draft]");
    var s = store();
    for (var i = 0; i < boxes.length; i++) {
      var box = boxes[i];
      var key = DRAFT + box.getAttribute("data-hp-draft");
      var form = box.form;
      var sent = form && form.querySelector("[data-hp-sent]");
      try {
        if (s && (sent || box.hasAttribute("data-hp-forget"))) {
          s.removeItem(key);
        } else if (s && !box.value) {
          box.value = s.getItem(key) || "";
        }
      } catch (err) {
        /* unreadable storage: nothing to restore. */
      }
    }
  }

  document.addEventListener("input", function (event) {
    var box = event.target;
    if (box && box.matches && box.matches("textarea[data-hp-draft]")) {
      keep(box);
    }
  });
  document.addEventListener("htmx:afterSwap", function (event) {
    settle(event.detail.elt.parentNode || document);
  });
  document.addEventListener("DOMContentLoaded", function () {
    settle(document);
  });

  /* Back and End are disabled while a write is in flight, or the result is never seen. */
  document.addEventListener("htmx:beforeRequest", function (event) {
    if (event.detail.requestConfig && event.detail.requestConfig.verb !== "get") {
      document.body.classList.add("is-sending");
    }
  });
  document.addEventListener("htmx:afterRequest", function () {
    document.body.classList.remove("is-sending");
  });

  /* ---- A tap on the answer region always says what became of it ----
   * The outcome banner is scrolled into view if the thumb left the region's top off screen,
   * and a tap that got no reply at all (a dropped link) shows the region's own notice. */
  function showOutcome(banner) {
    var top = banner.getBoundingClientRect().top;
    var bar = document.querySelector(".topbar");
    var above = bar ? bar.getBoundingClientRect().bottom : 0;
    if (top < above || top > window.innerHeight * 0.6) {
      banner.scrollIntoView({ block: "start" });
    }
  }

  document.addEventListener("htmx:afterSwap", function (event) {
    var config = event.detail.requestConfig;
    var region = document.getElementById("answer");
    if (!region || !config || config.verb === "get") {
      return;
    }
    var banner = region.querySelector(".answer > .banner:not([hidden])");
    if (banner) {
      showOutcome(banner);
    }
  });

  function lost(event) {
    var elt = event.detail && event.detail.elt;
    var region = elt && elt.closest ? elt.closest("#answer") : null;
    var notice = region ? region.querySelector("[data-hp-lost]") : null;
    if (notice) {
      notice.hidden = false;
      showOutcome(notice);
    }
  }
  document.addEventListener("htmx:sendError", lost);
  document.addEventListener("htmx:timeout", lost);
  document.addEventListener("htmx:responseError", lost);

  document.addEventListener("visibilitychange", rearm);
  /* bfcache restore and iOS standalone resume skip visibilitychange. The restored
   * DOM is whatever was on screen when the page was left, so it says so until the
   * re-fetch replaces it. */
  window.addEventListener("pageshow", function (event) {
    if (event.persisted) {
      var nodes = document.querySelectorAll("[data-hp-poll]");
      for (var i = 0; i < nodes.length; i++) {
        nodes[i].classList.add("is-stale");
      }
      document.body.classList.remove("is-sending");
      rearm();
    }
  });

  /* ---- The composer dock ----
   * The composer is fixed to the bottom. The page reserves the dock's real height, and
   * while the iOS keyboard is open the dock rides on top of it: the layout viewport keeps
   * its height there, so the gap under the visual viewport is the keyboard. */
  var root = document.documentElement;
  var docking = false;
  var dockedKb = -1;
  var dockedH = -1;
  var watched = null;
  // The dock grows with iPhone Text Size and with any banner in it; nothing else says so.
  var sizes = typeof window.ResizeObserver === "function" ? new window.ResizeObserver(redock) : null;

  function dock() {
    docking = false;
    var bar = document.getElementById("composer");
    if (!bar) {
      return;
    }
    if (sizes && watched !== bar) {
      if (watched) {
        sizes.unobserve(watched);
      }
      sizes.observe(bar);
      watched = bar;
    }
    var view = window.visualViewport;
    var covered = view ? Math.max(0, Math.round(window.innerHeight - view.height - view.offsetTop)) : 0;
    var height = bar.offsetHeight;
    var moved = covered !== dockedKb || height !== dockedH;
    // Measured before the dock moves: a reader at the end stays there as the keyboard opens.
    var wasEnd = moved && atEnd();
    dockedKb = covered;
    dockedH = height;
    root.style.setProperty("--kb", covered + "px");
    root.classList.toggle("is-keyboard", covered > 0);
    root.style.setProperty("--dock-h", height + "px");
    if (wasEnd) {
      toEnd();
    }
  }

  function redock() {
    if (!docking) {
      docking = true;
      window.requestAnimationFrame(dock);
    }
  }

  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", redock);
    window.visualViewport.addEventListener("scroll", redock);
  }
  window.addEventListener("resize", redock);
  document.addEventListener("focusin", redock);
  document.addEventListener("focusout", redock);
  document.addEventListener("input", redock);
  document.addEventListener("htmx:afterSettle", redock);

  /* ---- An armed interrupt disarms itself ----
   * After data-hp-disarm seconds the answer region is fetched afresh, which draws it
   * unarmed. Never under a thumb: a touch in the last second and a half puts it off,
   * so a tap meant for the confirm cannot land on whatever replaces it. */
  var lastTouch = 0;
  document.addEventListener("pointerdown", function () {
    lastTouch = Date.now();
  }, true);

  function disarmLater(node, seconds) {
    window.setTimeout(function () {
      var region = node.closest("[data-hp-poll]");
      if (!node.isConnected || !region || typeof window.htmx === "undefined") {
        return;
      }
      if (Date.now() - lastTouch < 1500 || document.body.classList.contains("is-sending")) {
        disarmLater(node, 1.5);
        return;
      }
      window.htmx.ajax("GET", region.getAttribute("data-hp-poll"), { target: region, swap: "outerHTML" });
    }, seconds * 1000);
  }

  function armTimers(root) {
    var nodes = root.querySelectorAll ? root.querySelectorAll("[data-hp-disarm]") : [];
    for (var i = 0; i < nodes.length; i++) {
      if (!nodes[i].hasAttribute("data-hp-timed")) {
        nodes[i].setAttribute("data-hp-timed", "");
        disarmLater(nodes[i], Number(nodes[i].getAttribute("data-hp-disarm")) || 5);
      }
    }
  }

  document.addEventListener("htmx:afterSettle", function () {
    armTimers(document);
  });

  /* ---- A details the reader opened stays open across the polls that redraw it ----
   * A fold, a tool's result or any other disclosure in the view: each is keyed by the turn it
   * sits in and its own name there (a tool call's id), else its place inside that turn, and
   * reopened before the reader's place is held. A run of tool calls is named by its first call,
   * so it keeps its key as calls are added to it. */
  var unfolded = [];

  function detailsKey(details) {
    var unit = details.closest(".turn");
    var inside = unit ? unit.querySelectorAll("details") : [];
    var own = details.getAttribute("data-hp-key");
    return (unit ? unitKey(unit) : "") + "#" + (own || Array.prototype.indexOf.call(inside, details));
  }

  document.addEventListener("htmx:beforeSwap", function (event) {
    var target = event.detail.target;
    if (!target || target.id !== "agent-view") {
      return;
    }
    unfolded = [];
    var open = target.querySelectorAll("details[open]");
    for (var i = 0; i < open.length; i++) {
      unfolded.push(detailsKey(open[i]));
    }
  });

  document.addEventListener("htmx:afterSwap", function () {
    if (!unfolded.length) {
      return;
    }
    var all = document.querySelectorAll("#agent-view details");
    for (var i = 0; i < all.length; i++) {
      if (unfolded.indexOf(detailsKey(all[i])) !== -1) {
        // A lone call that became the first of a run is now inside a fold: open that too.
        for (var d = all[i]; d; d = d.parentElement ? d.parentElement.closest("details") : null) {
          d.open = true;
        }
      }
    }
  });

  /* ---- Chat and Pane open on their newest line and follow it only while the reader is there ----
   * Both read oldest first, so the end of the page is the newest turn or terminal line, just
   * above the dock. Whether to follow is decided before each swap, from where the reader is
   * then; a reader who scrolled up keeps what they are reading where it was. A page that opens
   * on a question stays at the top, where the question is, until the reader moves. */
  // How far under the dock's top edge the newest line may end and still count as the end.
  var END_SLACK = 120;
  var swapEnd = false;
  var swapMark = null;
  var opening = true;

  // A chat turn, or a line of the terminal mirror (its status strip is not scrollback).
  var READ_UNITS = "#agent-view .turn, #agent-view > .pane:not(.pane--status) > .pane__line";
  // What a poll may not move under the reader: those, and the feed's cards, which re-sort.
  var ANCHORS = READ_UNITS + ", #feed-list .card";

  function readUnits() {
    return document.querySelectorAll(READ_UNITS);
  }

  function dockTop() {
    var bar = document.getElementById("composer");
    return bar ? bar.getBoundingClientRect().top : window.innerHeight;
  }

  function atEnd() {
    var turns = readUnits();
    var last = turns[turns.length - 1];
    return !last || last.getBoundingClientRect().bottom <= dockTop() + END_SLACK;
  }

  function asking() {
    var region = document.getElementById("answer");
    return !!(region && !region.classList.contains("answer--quiet"));
  }

  function toEnd() {
    if (readUnits().length && !(opening && asking())) {
      window.scrollTo(0, root.scrollHeight);
    }
  }

  // A card names its agent; a turn or a line is known by its text.
  function unitKey(unit) {
    return unit.getAttribute("data-hp-key") || String(unit.textContent).slice(0, 160);
  }

  function barBottom() {
    var bar = document.querySelector(".topbar");
    return bar ? bar.getBoundingClientRect().bottom : 0;
  }

  /* What to hold in place across a swap: the first unit wholly in view under the top bar,
   * else the first still on screen at all, as when one turn is taller than the screen. */
  function mark() {
    var units = document.querySelectorAll(ANCHORS);
    var under = barBottom();
    var partly = null;
    for (var i = 0; i < units.length; i++) {
      var box = units[i].getBoundingClientRect();
      if (box.bottom <= 0) {
        continue;
      }
      var held = { key: unitKey(units[i]), index: i, top: box.top };
      if (box.top >= under && box.top < window.innerHeight) {
        return held;
      }
      partly = partly || held;
    }
    return partly;
  }

  /* The same unit after the swap: same key, nearest its old place, since both windows drop
   * their oldest as new ones arrive and the feed re-sorts its cards. */
  function hold(held) {
    var units = document.querySelectorAll(ANCHORS);
    var best = -1;
    for (var i = 0; i < units.length; i++) {
      var nearer = best === -1 || Math.abs(i - held.index) < Math.abs(best - held.index);
      if (nearer && unitKey(units[i]) === held.key) {
        best = i;
      }
    }
    if (best !== -1) {
      window.scrollBy(0, units[best].getBoundingClientRect().top - held.top);
    }
  }

  function isRead(detail) {
    return !!(detail.requestConfig && detail.requestConfig.verb === "get");
  }

  document.addEventListener("htmx:beforeSwap", function (event) {
    if (!isRead(event.detail)) {
      return;
    }
    // Only the agent view follows its end; the feed is read from the top.
    swapEnd = !!document.getElementById("agent-view") && atEnd();
    swapMark = swapEnd ? null : mark();
  });

  document.addEventListener("htmx:afterSwap", function (event) {
    if (!isRead(event.detail)) {
      return;
    }
    if (opening && asking()) {
      window.scrollTo(0, 0);
    } else if (swapEnd) {
      toEnd();
    } else if (swapMark) {
      hold(swapMark);
    }
    swapMark = null;
    // The page has opened once both the view and the answer region have arrived.
    if (document.getElementById("answer") && readUnits().length) {
      opening = false;
    }
  });

  function opened() {
    opening = false;
  }
  ["pointerdown", "wheel", "keydown"].forEach(function (name) {
    document.addEventListener(name, opened, { capture: true, passive: true });
  });

  /* Sending is reading the end: the reply lands there. */
  document.addEventListener("htmx:beforeRequest", function (event) {
    var elt = event.detail.elt;
    if (elt && elt.id === "composer") {
      toEnd();
    }
  });

  /* ---- Keep the columns: the pane unwrapped, remembered on this device ---- */
  var COLUMNS = "hp-columns";

  function local() {
    try {
      return window.localStorage;
    } catch (err) {
      return null;
    }
  }

  function showColumns(button, on) {
    var host = button.closest("main");
    if (host) {
      host.classList.toggle("is-columns", on);
    }
    button.setAttribute("aria-pressed", on ? "true" : "false");
  }

  var columnsButton = document.querySelector("[data-hp-columns]");
  if (columnsButton) {
    var kept = false;
    try {
      kept = !!local() && local().getItem(COLUMNS) === "1";
    } catch (err) {
      kept = false;
    }
    showColumns(columnsButton, kept);
    columnsButton.parentNode.hidden = false;
    columnsButton.addEventListener("click", function () {
      var on = columnsButton.getAttribute("aria-pressed") !== "true";
      showColumns(columnsButton, on);
      try {
        if (local()) {
          local().setItem(COLUMNS, on ? "1" : "0");
        }
      } catch (err) {
        /* blocked storage: the choice lasts this page only. */
      }
    });
  }

  /* ---- VoiceOver: a poll never speaks, moves focus or leaves a half-drawn region ----
   * A region is redrawn whole every few seconds. While it is swapped its host says it is busy,
   * the control that had focus gets it back in the new drawing, and the only things said
   * aloud go through the page's one live region: what a write did, and a new message. */
  var busyHosts = [];
  var refocus = null;
  var writeTarget = "";
  var newestSaid = null;

  function say(text) {
    var live = document.getElementById("hp-live");
    if (!live || !text) {
      return;
    }
    // Emptied first, so the same words twice are still news.
    live.textContent = "";
    window.setTimeout(function () {
      live.textContent = text;
    }, 50);
  }

  // As laid out, so the parts of a banner or a turn are said as separate words.
  function words(node) {
    return String(node.innerText || node.textContent).replace(/\s+/g, " ").trim();
  }

  // What a focused control is, so its twin can be found in the region's next drawing.
  function focusMark(target) {
    var active = document.activeElement;
    if (!active || active === document.body || !target.contains(active)) {
      return null;
    }
    var form = active.form || null;
    return {
      region: target.id,
      tag: active.tagName,
      text: words(active),
      action: form ? form.getAttribute("action") : "",
      key: form && form.elements.key ? form.elements.key.value : "",
      index: Array.prototype.indexOf.call(target.querySelectorAll(active.tagName), active),
    };
  }

  function refocusIn(mark) {
    var region = document.getElementById(mark.region);
    if (!region || (document.activeElement && region.contains(document.activeElement))) {
      return;
    }
    var all = region.querySelectorAll(mark.tag);
    var best = null;
    for (var i = 0; i < all.length; i++) {
      var form = all[i].form || null;
      var same = words(all[i]) === mark.text
        && (form ? form.getAttribute("action") : "") === mark.action
        && (form && form.elements.key ? form.elements.key.value : "") === mark.key;
      if (same && (!best || Math.abs(i - mark.index) < Math.abs(best.at - mark.index))) {
        best = { node: all[i], at: i };
      }
    }
    if (best) {
      best.node.focus({ preventScroll: true });
    }
  }

  document.addEventListener("htmx:beforeSwap", function (event) {
    var target = event.detail.target;
    var xhr = event.detail.xhr;
    // A hidden page's stopped poll swaps nothing, and nothing would settle to clear the mark.
    if (!target || !event.detail.shouldSwap || (xhr && xhr.getResponseHeader("HX-Reswap") === "none")) {
      return;
    }
    var host = target.parentElement;
    if (host) {
      host.setAttribute("aria-busy", "true");
      busyHosts.push(host);
    }
    refocus = focusMark(target);
    writeTarget = isRead(event.detail) ? "" : target.id;
  });

  // The newest message in the chat, if the newest thing in it is the agent speaking.
  function newestMessage() {
    var units = document.querySelectorAll("#agent-view.chat .turn");
    var last = units[units.length - 1];
    return last && last.classList.contains("turn--agent") ? last : null;
  }

  document.addEventListener("htmx:afterSwap", function (event) {
    if (refocus) {
      refocusIn(refocus);
      refocus = null;
    }
    if (writeTarget) {
      var region = document.getElementById(writeTarget);
      var outcome = region ? region.querySelector(".banner[data-hp-sent]") : null;
      if (outcome) {
        say(words(outcome));
      }
      writeTarget = "";
      return;
    }
    if (!isRead(event.detail) || !document.querySelector("#agent-view.chat")) {
      return;
    }
    var newest = newestMessage();
    var key = newest ? unitKey(newest) : "";
    // The first drawing is the page opening, not news.
    if (newestSaid !== null && newest && key !== newestSaid) {
      say("New message: " + words(newest).replace(/^Agent:\s*/, "").slice(0, 140));
    }
    newestSaid = key;
  });

  document.addEventListener("htmx:afterSettle", function () {
    while (busyHosts.length) {
      busyHosts.pop().removeAttribute("aria-busy");
    }
  });

  /* ---- Service worker and the "new build" offer ---- */

  var worker = "serviceWorker" in navigator ? navigator.serviceWorker : null;
  var buildMeta = document.querySelector('meta[name="hp-build"]');
  var baseline = buildMeta ? buildMeta.getAttribute("content") : null;
  var swapping = false;

  if (worker) {
    worker.register("/sw.js", { scope: "/" }).catch(function () {
      /* No worker means no offline shell and no push; the page itself still works. */
    });
    /* Reload only after the user's tap asked for the swap, never on a first install. */
    worker.addEventListener("controllerchange", function () {
      if (swapping) {
        window.location.reload();
      }
    });
  }

  /* A missing header says nothing; only a second, different stamp means the build moved. */
  function noteBuild(stamp) {
    if (!stamp) {
      return;
    }
    if (baseline === null) {
      baseline = stamp;
      return;
    }
    if (stamp !== baseline) {
      var bar = document.getElementById("hp-update");
      if (bar) {
        bar.hidden = false;
      }
    }
  }

  document.addEventListener("htmx:afterRequest", function (event) {
    var xhr = event.detail.xhr;
    if (xhr) {
      noteBuild(xhr.getResponseHeader("HP-Build"));
    }
  });

  function update() {
    if (!worker) {
      window.location.reload();
      return;
    }
    worker.getRegistration().then(function (registration) {
      if (registration && registration.waiting) {
        swapping = true;
        registration.waiting.postMessage({ type: "hp-skip-waiting" });
        return;
      }
      /* No new worker waiting: navigations are network-first, so a reload is the new build. */
      window.location.reload();
    });
  }

  var updateButton = document.getElementById("hp-update-button");
  if (updateButton) {
    updateButton.addEventListener("click", update);
  }

  /* ---- Web Push ---- */

  function postJson(url, body) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (response) {
      return response.json().then(function (data) {
        return response.ok ? data : Promise.reject(data.error || response.status);
      });
    });
  }

  /* applicationServerKey takes bytes everywhere and a string only in some browsers. */
  function keyBytes(base64url) {
    var padded = String(base64url).replace(/-/g, "+").replace(/_/g, "/");
    var raw = atob(padded + "=".repeat((4 - (padded.length % 4)) % 4));
    var bytes = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i += 1) {
      bytes[i] = raw.charCodeAt(i);
    }
    return bytes;
  }

  function pushManager() {
    if (!worker || !("PushManager" in window)) {
      return Promise.reject("this browser has no Web Push here (on iPhone, open the Home Screen app)");
    }
    return worker.ready.then(function (registration) {
      return registration.pushManager;
    });
  }

  function currentSubscription() {
    return pushManager().then(function (manager) {
      return manager.getSubscription();
    });
  }

  /* The server confirms by echoing the endpoint; nothing else counts as "on". */
  function register(subscription) {
    return postJson("/push/subscribe", subscription.toJSON()).then(function (answer) {
      return answer.endpoint === subscription.endpoint ? answer : Promise.reject("not confirmed");
    });
  }

  /* Re-post on every load, so a store that lost this device learns it again. */
  if (worker && "Notification" in window && Notification.permission === "granted") {
    currentSubscription().then(function (subscription) {
      return subscription ? register(subscription) : null;
    }).catch(function () {});
  }

  function setText(id, text) {
    var node = document.getElementById(id);
    if (node) {
      node.textContent = text;
    }
  }

  function showButtons(on) {
    var enable = document.getElementById("push-on");
    var disable = document.getElementById("push-off");
    if (enable) {
      enable.hidden = on;
    }
    if (disable) {
      disable.hidden = !on;
    }
  }

  function refreshDevice() {
    return currentSubscription().then(function (subscription) {
      if (!subscription) {
        setText("push-state", "Alerts are off on this device.");
        showButtons(false);
        return null;
      }
      return register(subscription).then(function (answer) {
        setText("push-state", answer.armed ? "Alerts are on for this device." : "This device is subscribed, but the box is not sending yet.");
        showButtons(true);
      });
    }).catch(function (reason) {
      setText("push-state", "Alerts are unavailable: " + String(reason));
      showButtons(false);
    });
  }

  function enableDevice() {
    Notification.requestPermission().then(function (permission) {
      if (permission !== "granted") {
        return Promise.reject("permission was not granted");
      }
      return fetch("/push/key", { credentials: "same-origin" }).then(function (response) {
        return response.ok ? response.json() : Promise.reject(response.status);
      });
    }).then(function (body) {
      return pushManager().then(function (manager) {
        return manager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(body.publicKey) });
      });
    }).then(register).then(refreshDevice).catch(function (reason) {
      setText("push-state", "Could not turn alerts on: " + String(reason));
    });
  }

  function disableDevice() {
    currentSubscription().then(function (subscription) {
      if (!subscription) {
        return null;
      }
      return postJson("/push/unsubscribe", { endpoint: subscription.endpoint }).then(function () {
        return subscription.unsubscribe();
      });
    }).then(refreshDevice).catch(function (reason) {
      setText("push-state", "Could not turn alerts off: " + String(reason));
    });
  }

  function showPolicy(policy) {
    var blocked = document.getElementById("pref-blocked");
    var done = document.getElementById("pref-done");
    if (blocked) {
      blocked.checked = policy.blocked === true;
    }
    if (done) {
      done.checked = policy.done === true;
    }
    setText("snooze-state", policy.snoozed ? "Finishes are quiet for now." : "Not snoozed.");
  }

  function wirePreference(id, key) {
    var box = document.getElementById(id);
    if (!box) {
      return;
    }
    box.addEventListener("change", function () {
      var body = {};
      body[key] = box.checked;
      /* The box shows what the server kept, not what was clicked. */
      postJson("/push/prefs", body).then(showPolicy).catch(function () {
        box.checked = !box.checked;
      });
    });
  }

  var panel = document.querySelector("[data-hp-push]");
  if (panel) {
    refreshDevice();
    var enable = document.getElementById("push-on");
    var disable = document.getElementById("push-off");
    if (enable) {
      enable.addEventListener("click", enableDevice);
    }
    if (disable) {
      disable.addEventListener("click", disableDevice);
    }
    wirePreference("pref-blocked", "blocked");
    wirePreference("pref-done", "done");
    var snoozes = document.querySelectorAll("[data-hp-snooze]");
    for (var s = 0; s < snoozes.length; s++) {
      snoozes[s].addEventListener("click", function (event) {
        var seconds = Number(event.currentTarget.getAttribute("data-hp-snooze"));
        postJson("/push/snooze", { seconds: seconds }).then(showPolicy).catch(function () {});
      });
    }
  }

  /* ---- Copy a code block ----
   * The clipboard write happens inside the tap, which is what iOS requires of it. Without the
   * clipboard API (an http origin, an old WebKit) the code is selected so the system menu can
   * copy it. */

  function selectText(node) {
    var range = document.createRange();
    range.selectNodeContents(node);
    var selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  }

  function copied(button, label) {
    button.textContent = label;
    window.setTimeout(function () {
      button.textContent = "Copy";
    }, 1500);
  }

  document.addEventListener("click", function (event) {
    var button = event.target && event.target.closest ? event.target.closest("[data-hp-copy]") : null;
    var block = button ? button.closest(".code") : null;
    var code = block ? block.querySelector("pre code") : null;
    if (!code) {
      return;
    }
    if (!navigator.clipboard || !window.isSecureContext) {
      selectText(code);
      copied(button, "Selected");
      return;
    }
    navigator.clipboard.writeText(code.textContent).then(
      function () {
        copied(button, "Copied");
      },
      function () {
        selectText(code);
        copied(button, "Selected");
      }
    );
  });

  /* ---- Files: a file opens on the line it was asked for ----
   * Centred, so the lines around it are read with it; its chunk is laid out on the way. Again
   * once loaded, since the browser's own jump to the #L anchor lands it under the bar. */
  var fileAt = document.querySelector(".src [data-hp-at]");
  function centreFileAt() {
    fileAt.scrollIntoView({ block: "center" });
  }
  if (fileAt) {
    centreFileAt();
    window.addEventListener("load", function () {
      window.setTimeout(centreFileAt, 0);
    }, { once: true });
  }

  /* ---- Seen: a finish the phone has shown is not news ---- */

  var seenHost = document.querySelector("[data-hp-seen]");
  function markSeen() {
    var session = seenHost ? seenHost.getAttribute("data-hp-seen") : "";
    if (session && !document.hidden) {
      fetch("/push/seen/" + encodeURIComponent(session), {
        method: "POST",
        credentials: "same-origin",
      }).catch(function () {});
    }
  }
  markSeen();
  document.addEventListener("visibilitychange", markSeen);
})();
