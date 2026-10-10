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

  function dock() {
    docking = false;
    var bar = document.getElementById("composer");
    if (!bar) {
      return;
    }
    var view = window.visualViewport;
    var covered = view ? Math.max(0, Math.round(window.innerHeight - view.height - view.offsetTop)) : 0;
    root.style.setProperty("--kb", covered + "px");
    root.classList.toggle("is-keyboard", covered > 0);
    root.style.setProperty("--dock-h", bar.offsetHeight + "px");
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

  /* ---- A folded chat turn the user opened stays open across the Chat tab's polls ---- */
  var unfolded = [];

  function foldKey(more) {
    var text = more.previousElementSibling;
    return text ? String(text.textContent).slice(0, 160) : "";
  }

  document.addEventListener("htmx:beforeSwap", function (event) {
    var target = event.detail.target;
    if (!target || target.id !== "agent-view") {
      return;
    }
    unfolded = [];
    var open = target.querySelectorAll(".turn__more[open]");
    for (var i = 0; i < open.length; i++) {
      unfolded.push(foldKey(open[i]));
    }
  });

  document.addEventListener("htmx:afterSwap", function () {
    if (!unfolded.length) {
      return;
    }
    var folds = document.querySelectorAll("#agent-view .turn__more");
    for (var i = 0; i < folds.length; i++) {
      if (unfolded.indexOf(foldKey(folds[i])) !== -1) {
        folds[i].open = true;
      }
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
