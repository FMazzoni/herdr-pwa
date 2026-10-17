/* herdr-pwa: what of the page's script has not moved to a section file of its own. It never
 * builds markup from strings and never evals. Regions are redrawn when the event stream says they
 * went stale; htmx trigger filters would need eval, so the server stops a hidden page's polls
 * (HTTP 286) and this script re-arms them. */
(function (hp) {
  "use strict";

  var logic = hp.logic;
  var bus = hp.bus;
  var refetch = hp.refetch;
  var isRead = hp.isRead;

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
    var kept = false;
    try {
      var device = hp.storage("localStorage");
      kept = !!device && device.getItem(COLUMNS) === "1";
    } catch (err) {
      kept = false;
    }
    showColumns(columnsButton, kept);
    columnsButton.parentNode.hidden = false;
    columnsButton.addEventListener("click", function () {
      var on = columnsButton.getAttribute("aria-pressed") !== "true";
      showColumns(columnsButton, on);
      try {
        var device = hp.storage("localStorage");
        if (device) {
          device.setItem(COLUMNS, on ? "1" : "0");
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

  // What is to be said in the next moment, so two things said in one swap are both heard.
  var toSay = [];

  function say(text) {
    var spoken = document.getElementById("hp-live");
    if (!spoken || !text) {
      return;
    }
    toSay.push(text);
    if (toSay.length > 1) {
      return;
    }
    // Emptied first, so the same words twice are still news.
    spoken.textContent = "";
    window.setTimeout(function () {
      spoken.textContent = toSay.join(". ");
      toSay = [];
    }, 50);
  }

  // As laid out, so the parts of a banner or a turn are said as separate words.
  function words(node) {
    return String(node.innerText || node.textContent).replace(/\s+/g, " ").trim();
  }

  // The one voice of the page, for news a section of its own has (a question waiting below).
  hp.voice = { say: say, words: words };

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

  function refocusIn(was) {
    var region = document.getElementById(was.region);
    if (!region || (document.activeElement && region.contains(document.activeElement))) {
      return;
    }
    var all = region.querySelectorAll(was.tag);
    var best = null;
    for (var i = 0; i < all.length; i++) {
      var form = all[i].form || null;
      var same = words(all[i]) === was.text
        && (form ? form.getAttribute("action") : "") === was.action
        && (form && form.elements.key ? form.elements.key.value : "") === was.key;
      if (same && (!best || Math.abs(i - was.index) < Math.abs(best.at - was.index))) {
        best = { node: all[i], at: i };
      }
    }
    if (best) {
      best.node.focus({ preventScroll: true });
    }
  }

  bus.on("swap:capture", function (detail) {
    var target = detail.target;
    var xhr = detail.xhr;
    // A hidden page's stopped poll swaps nothing, and nothing would settle to clear the mark.
    if (!target || !detail.shouldSwap || (xhr && xhr.getResponseHeader("HX-Reswap") === "none")) {
      return;
    }
    var host = target.parentElement;
    if (host) {
      host.setAttribute("aria-busy", "true");
      busyHosts.push(host);
    }
    refocus = focusMark(target);
    writeTarget = isRead(detail) ? "" : target.id;
  });

  // The newest message in the chat, if the newest thing in it is the agent speaking.
  function newestMessage() {
    var units = document.querySelectorAll("#agent-view.chat .turn");
    var last = units[units.length - 1];
    return last && last.classList.contains("turn--agent") ? last : null;
  }

  bus.on("swapped:announce", function (detail) {
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
    if (!isRead(detail) || !document.querySelector("#agent-view.chat")) {
      return;
    }
    var newest = newestMessage();
    var key = newest ? hp.chatend.unitKey(newest) : "";
    // The first drawing is the page opening, not news.
    if (newestSaid !== null && newest && key !== newestSaid) {
      say("New message: " + words(newest).replace(/^Agent:\s*/, "").slice(0, 140));
    }
    newestSaid = key;
  });

  // The conversation the page opened on is not news either.
  bus.on("ready:announce", function () {
    if (document.querySelector("#agent-view.chat")) {
      var newest = newestMessage();
      newestSaid = newest ? hp.chatend.unitKey(newest) : "";
    }
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

  /* ---- The stats strip keeps its details open across the redraws that refresh it ---- */
  var statsOpen = false;

  bus.on("swap:capture", function (detail) {
    var target = detail.target;
    if (target && target.id === "agent-stats") {
      var sheet = target.querySelector("details");
      statsOpen = !!(sheet && sheet.open);
    }
  });

  // After the page has its place, as it always has: reopening it first would move the hold.
  bus.on("swapped:settle", function () {
    var sheet = statsOpen ? document.querySelector("#agent-stats details") : null;
    statsOpen = false;
    if (sheet) {
      sheet.open = true;
    }
  });

  /* ---- Seen: a finish the phone has shown is not news ----
   * Said on load and again each time the page comes back into view. */

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

  /* The answer region is what the stream redraws for this agent, and it carries the status the
   * page shows; the status word and dot follow it, and a finish seen on screen is marked read. */
  var statusWord = document.getElementById("agent-status");
  var shownStatus = statusWord ? statusWord.textContent : "";
  document.addEventListener("htmx:afterSettle", function () {
    var region = document.getElementById("answer");
    var status = region ? region.getAttribute("data-hp-status") : null;
    if (!status || status === shownStatus) {
      return;
    }
    var word = document.getElementById("agent-status");
    var dot = document.getElementById("agent-dot");
    if (word) {
      word.textContent = status;
    }
    if (dot) {
      dot.className = "dot dot--" + status;
    }
    if (logic.finishedNow(shownStatus, status)) {
      markSeen();
    }
    shownStatus = status;
  });

  /* ---- The status bubble: a crest crosses its words while the agent works; its timer ticks ----
   * The server draws the bubble's state, words and clocks on every redraw; between redraws this
   * carries the timer on, says "No activity" once the log has been still too long (a log that
   * does not move redraws nothing), and runs at most one frame loop, only while the bubble is
   * working, on screen, in a visible page and allowed to move. Under Reduce Motion, or a motion
   * style that is still, the words stand still and the timer still ticks. VoiceOver is told only
   * when the state changes: working, quiet, needs you, done. */
  var stillness = window.matchMedia("(prefers-reduced-motion: reduce)");
  var bubble = {
    node: null, stretch: "", start: 0, drawnAt: 0, ran: null, quiet: null, after: NaN,
    motion: null, glyphs: [], frame: 0, tick: 0, seen: false, observer: null,
  };
  // The state last told to VoiceOver, so a redraw of the same state says nothing.
  var bubbleState = null;

  // A number the server wrote into an attribute, or null where it wrote none.
  function attrNumber(raw) {
    return raw === null || raw === "" ? null : Number(raw);
  }

  function motionOf(node) {
    var name = node.getAttribute("data-working-motion") || "";
    return Object.prototype.hasOwnProperty.call(logic.MOTIONS, name) ? logic.MOTIONS[name] : null;
  }

  // The words as one span per letter, which the crest colours one by one; VoiceOver skips them.
  function layGlyphs(node, motion) {
    var words = node.querySelector(".bubble__text");
    if (!words) {
      return [];
    }
    var glyphs = Array.from(words.textContent).map(function (letter) {
      var glyph = document.createElement("span");
      glyph.className = "bubble__glyph";
      glyph.textContent = letter;
      return glyph;
    });
    words.replaceChildren.apply(words, glyphs);
    node.classList.toggle("is-lifting", motion.rise > 0);
    node.style.setProperty("--working-rise", motion.rise + "em");
    return glyphs;
  }

  function drawCrest(now) {
    var b = bubble;
    var count = b.glyphs.length;
    var phase = ((now - b.start) % b.motion.period) / b.motion.period;
    b.glyphs.forEach(function (glyph, i) {
      var middle = i + 0.5;
      var side = logic.glow(middle, count, phase, b.motion.reach);
      glyph.style.setProperty("--glyph-wave", logic.wave(middle, count, phase, b.motion.reach).toFixed(3));
      // Three stops as two blends: the first two, then that and the third.
      glyph.style.setProperty("--glyph-near", Math.min(1, side * 2).toFixed(3));
      glyph.style.setProperty("--glyph-far", Math.max(0, side * 2 - 1).toFixed(3));
    });
  }

  function runCrest() {
    var b = bubble;
    var go = b.glyphs.length > 0 && b.seen && !document.hidden;
    if (go && !b.frame) {
      b.frame = window.requestAnimationFrame(function (now) {
        b.frame = 0;
        if (!b.node || !b.node.isConnected) {
          stopBubble();
          return;
        }
        drawCrest(now);
        runCrest();
      });
    } else if (!go && b.frame) {
      window.cancelAnimationFrame(b.frame);
      b.frame = 0;
    }
  }

  function stopCrest() {
    var b = bubble;
    if (b.frame) {
      window.cancelAnimationFrame(b.frame);
      b.frame = 0;
    }
    if (b.observer) {
      b.observer.disconnect();
      b.observer = null;
    }
    var words = b.node ? b.node.querySelector(".bubble__text") : null;
    if (words && b.glyphs.length) {
      // The letters' own text, back as one run of words.
      var text = words.textContent;
      words.textContent = text;
    }
    b.glyphs = [];
  }

  function stopBubble() {
    stopCrest();
    if (bubble.tick) {
      window.clearTimeout(bubble.tick);
      bubble.tick = 0;
    }
    bubble.node = null;
  }

  // The bubble's own words, and VoiceOver's, once this page has seen the log stay still too long.
  function showQuiet(node, seconds) {
    var words = node.querySelector(".bubble__text");
    var described = node.querySelector(".sr-only");
    if (words) {
      words.textContent = logic.quietFor(seconds);
    }
    if (described) {
      described.textContent = "No recent activity";
    }
  }

  function goQuiet(node, seconds) {
    stopCrest();
    node.classList.remove("bubble--working");
    node.classList.add("bubble--quiet");
    node.setAttribute("data-hp-bubble", "quiet");
    node.setAttribute("data-hp-said", "No recent activity");
    showQuiet(node, seconds);
    tellBubble();
  }

  // Once a second, on the second: the timer, and whether the turn has gone quiet.
  function tickBubble() {
    var b = bubble;
    b.tick = 0;
    if (!b.node || !b.node.isConnected) {
      stopBubble();
      return;
    }
    var since = window.performance.now() - b.drawnAt;
    var time = b.node.querySelector(".bubble__time");
    if (time && b.ran !== null) {
      time.textContent = logic.elapsed(b.ran + since / 1000);
    }
    var state = b.node.getAttribute("data-hp-bubble");
    if (b.quiet !== null && state === "working" && logic.goneQuiet(b.quiet, since, b.after)) {
      goQuiet(b.node, b.quiet + since / 1000);
    } else if (b.quiet !== null && state === "quiet") {
      showQuiet(b.node, b.quiet + since / 1000);
    }
    if (!document.hidden) {
      b.tick = window.setTimeout(tickBubble, 1000 - (since % 1000));
    }
  }

  /* Says the bubble's state when it is not the one last said: the first drawing is the page
   * opening, not news. A finished turn's receipt settles in where the bubble was. */
  function tellBubble() {
    var shown = document.querySelector("#chat-note [data-hp-bubble]");
    var state = shown ? shown.getAttribute("data-hp-bubble") : "";
    if (bubbleState !== null && state !== bubbleState) {
      if (shown) {
        say(shown.getAttribute("data-hp-said") || "");
      }
      if (state === "done" && bubbleState !== "") {
        shown.classList.add("is-folding");
      }
    }
    bubbleState = state;
  }

  function startBubble() {
    var node = document.querySelector("#chat-note .bubble[data-hp-bubble]");
    if (node === bubble.node) {
      return;
    }
    stopBubble();
    if (!node) {
      return;
    }
    var b = bubble;
    var stretch = node.getAttribute("data-hp-stretch") || "";
    b.drawnAt = window.performance.now();
    // A redraw of the same stretch carries the crest on from where it was.
    if (stretch !== b.stretch) {
      b.stretch = stretch;
      b.start = b.drawnAt;
    }
    b.node = node;
    b.ran = attrNumber(node.getAttribute("data-hp-ran"));
    b.quiet = attrNumber(node.getAttribute("data-hp-quiet"));
    b.after = attrNumber(node.getAttribute("data-hp-quiet-after"));
    b.motion = motionOf(node);
    if (node.getAttribute("data-hp-bubble") === "working" && b.motion && !stillness.matches) {
      b.glyphs = layGlyphs(node, b.motion);
      drawCrest(b.drawnAt);
      b.observer = new IntersectionObserver(function (entries) {
        b.seen = entries[entries.length - 1].isIntersecting;
        runCrest();
      });
      b.observer.observe(node);
    }
    tickBubble();
  }

  function wakeBubble() {
    runCrest();
    if (bubble.node && !bubble.tick && !document.hidden) {
      tickBubble();
    }
  }

  function showBubble() {
    tellBubble();
    startBubble();
  }
  bus.on("ready:announce", showBubble);
  bus.on("swapped:announce", showBubble);
  document.addEventListener("visibilitychange", wakeBubble);
  window.addEventListener("pageshow", wakeBubble);
  stillness.addEventListener("change", function () {
    stopBubble();
    startBubble();
  });
})(window.hp);
