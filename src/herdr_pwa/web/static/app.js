/* herdr-pwa: the page's shell, last of its scripts: a failure fragment swapped in, the service
 * worker and its "new build" offer, Web Push, copying a code block, Keep the columns, and a file
 * opened on its line. Each other concern has a script of its own (assets.SCRIPTS). */
(function (hp) {
  "use strict";

  var bus = hp.bus;

  /* htmx 2 refuses to swap a non-2xx; a fragment the server marked is the honest
   * failure screen, and it carries the poll that heals the page. */
  bus.on("swap:admit", function (detail) {
    var xhr = detail.xhr;
    if (xhr && xhr.status >= 400 && xhr.getResponseHeader("HP-Fragment") === "1") {
      detail.shouldSwap = true;
      detail.isError = false;
    }
  });

  /* ---- Keep the columns: the pane unwrapped, remembered on this device ---- */
  var COLUMNS = "hp-columns";

  function showColumns(button, on) {
    var host = button.closest("main");
    if (host) {
      host.classList.toggle("is-columns", on);
    }
    button.setAttribute("aria-pressed", on ? "true" : "false");
  }

  var columnsButton = document.querySelector("[data-hp-columns]");
  if (columnsButton) {
    showColumns(columnsButton, hp.kept(COLUMNS));
    columnsButton.parentNode.hidden = false;
    columnsButton.addEventListener("click", function () {
      var on = columnsButton.getAttribute("aria-pressed") !== "true";
      showColumns(columnsButton, on);
      hp.keep(COLUMNS, on);
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

  /* The endpoint this page registered last, sent as "replaces" so a re-subscribe swaps it out. */
  var REGISTERED = "hp-push-endpoint";
  /* The owner turned alerts off here, so the banner does not nag about a choice. */
  var DECLINED = "hp-alerts-declined";
  var HERD_TAG = "herd";
  var NO_PUSH = "this browser has no Web Push here (on iPhone, open the Home Screen app)";
  var DEVICE_TEXT = {
    on: "Alerts are on for this device.",
    waiting: "This device is subscribed, but the server is not sending alerts yet.",
    off: "Alerts are off on this device.",
    unknown: "Alerts are off on this device: the server no longer has it.",
    denied: "Alerts are blocked for this app; allow its notifications in Settings.",
  };
  var panel = document.querySelector("[data-hp-push]");
  var banner = document.getElementById("hp-alerts-off");

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
      return Promise.reject(NO_PUSH);
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

  function registered() {
    try {
      var device = hp.storage("localStorage");
      return device ? device.getItem(REGISTERED) : null;
    } catch (err) {
      return null;
    }
  }

  function remember(endpoint) {
    try {
      var device = hp.storage("localStorage");
      if (device) {
        device.setItem(REGISTERED, endpoint);
      }
    } catch (err) {
      /* blocked storage: the next subscribe swaps nothing, and the push service prunes the old one. */
    }
  }

  function setText(id, text) {
    var node = document.getElementById(id);
    if (node) {
      node.textContent = text;
    }
  }

  function showDevices(count) {
    if (typeof count === "number") {
      setText("push-devices", count + (count === 1 ? " device" : " devices") + " subscribed on this server.");
    }
  }

  /* The server confirms by echoing the endpoint; nothing else counts as "on". */
  function register(subscription) {
    var body = subscription.toJSON();
    var previous = registered();
    if (previous && previous !== subscription.endpoint) {
      body.replaces = previous;
    }
    return postJson("/push/subscribe", body).then(function (answer) {
      if (answer.endpoint !== subscription.endpoint) {
        return Promise.reject("not confirmed");
      }
      remember(answer.endpoint);
      showDevices(answer.devices);
      return answer;
    });
  }

  /* The server sends no clear, since a push that shows nothing costs the subscription, so the
   * page closes the herd alert once the server owes none; a ping keeps its own tag. */
  function closeHerdAlert() {
    if (!worker) {
      return;
    }
    worker.ready.then(function (registration) {
      return typeof registration.getNotifications === "function" ? registration.getNotifications({ tag: HERD_TAG }) : [];
    }).then(function (open) {
      open.forEach(function (notification) {
        notification.close();
      });
    }).catch(function () {
      /* No notifications to list here, so none is left showing to close. */
    });
  }

  /* This device as the box sees it: "on", "waiting" (held, box not sending), "off", "denied",
   * or "unknown" (subscribed here, but the box does not hold it, so nothing arrives). */
  function deviceState() {
    if (!worker || !("PushManager" in window) || !("Notification" in window)) {
      return Promise.reject(NO_PUSH);
    }
    if (Notification.permission !== "granted") {
      return Promise.resolve(Notification.permission === "denied" ? "denied" : "off");
    }
    return currentSubscription().then(function (subscription) {
      if (!subscription) {
        return "off";
      }
      return postJson("/push/device", { endpoint: subscription.endpoint }).then(function (answer) {
        showDevices(answer.devices);
        /* Strictly false: a blocked agent's alert stays on the lock screen until it is answered. */
        if (answer.herdAlert === false) {
          closeHerdAlert();
        }
        if (!answer.known) {
          return "unknown";
        }
        remember(subscription.endpoint);
        return answer.armed ? "on" : "waiting";
      });
    });
  }

  /* Only a Home Screen app can get alerts on iPhone, so only there is their absence news. */
  function standalone() {
    return navigator.standalone === true || (!!window.matchMedia && window.matchMedia("(display-mode: standalone)").matches);
  }

  function showButtons(canEnable, canDisable) {
    var enable = document.getElementById("push-on");
    var disable = document.getElementById("push-off");
    if (enable) {
      enable.hidden = !canEnable;
    }
    if (disable) {
      disable.hidden = !canDisable;
    }
  }

  /* The alerts page says it in its own words, so the banner is for every other page. */
  function showBanner(state) {
    if (!banner) {
      return;
    }
    var off = state === "off" || state === "unknown" || state === "denied";
    banner.hidden = !off || !!panel || !standalone() || hp.kept(DECLINED);
    var denied = state === "denied";
    setText("hp-alerts-off-what", denied ? "Alerts are off on this phone: allow herdr's notifications in Settings." : "Alerts are off on this phone.");
    var button = document.getElementById("hp-alerts-on");
    if (button) {
      button.hidden = denied;
    }
  }

  function showDevice(state) {
    setText("push-state", DEVICE_TEXT[state]);
    showButtons(state === "off" || state === "unknown", state === "on" || state === "waiting");
    showBanner(state);
  }

  function refreshDevice() {
    return deviceState().then(showDevice).catch(function (reason) {
      setText("push-state", "Alerts are unavailable: " + String(reason));
      showButtons(true, false);
    });
  }

  /* Asks the server about this endpoint only; the banner itself shows only in a Home Screen app. */
  function checkDevice() {
    if (panel) {
      refreshDevice();
      return;
    }
    if (!banner) {
      return;
    }
    deviceState().then(showBanner).catch(function () {
      /* No Web Push here, or push is off on the box: nothing the banner's button can fix. */
    });
  }

  function enableDevice() {
    return Notification.requestPermission().then(function (permission) {
      if (permission !== "granted") {
        return Promise.reject("permission was not granted");
      }
      return fetch("/push/key", { credentials: "same-origin" }).then(function (response) {
        return response.ok ? response.json() : Promise.reject(response.status);
      });
    }).then(function (body) {
      return pushManager().then(function (manager) {
        /* One the box does not hold may be one the push service dropped, so start afresh. */
        return manager.getSubscription().then(function (stale) {
          return stale ? stale.unsubscribe() : null;
        }).then(function () {
          return manager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(body.publicKey) });
        });
      });
    }).then(register).then(function (answer) {
      hp.keep(DECLINED, false);
      /* The subscribe's own answer is the confirmation; asking again would post a second one. */
      showDevice(answer.armed ? "on" : "waiting");
    });
  }

  function disableDevice() {
    currentSubscription().then(function (subscription) {
      if (!subscription) {
        return null;
      }
      return postJson("/push/unsubscribe", { endpoint: subscription.endpoint }).then(function (answer) {
        showDevices(answer.devices);
        return subscription.unsubscribe();
      });
    }).then(function () {
      hp.keep(DECLINED, true);
      return refreshDevice();
    }).catch(function (reason) {
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

  checkDevice();
  /* A Home Screen app resumes rather than reloads, and its owner may be back from Settings. */
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) {
      checkDevice();
    }
  });
  var bannerButton = document.getElementById("hp-alerts-on");
  if (bannerButton) {
    bannerButton.addEventListener("click", function () {
      enableDevice().catch(function (reason) {
        setText("hp-alerts-off-what", "Could not turn alerts on: " + String(reason));
      });
    });
  }
  if (panel) {
    var enable = document.getElementById("push-on");
    var disable = document.getElementById("push-off");
    if (enable) {
      enable.addEventListener("click", function () {
        enableDevice().catch(function (reason) {
          setText("push-state", "Could not turn alerts on: " + String(reason));
        });
      });
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
        postJson("/push/snooze", { seconds: seconds }).then(showPolicy).catch(function (reason) {
          setText("snooze-state", "Could not snooze: " + String(reason));
        });
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
})(window.hp);
