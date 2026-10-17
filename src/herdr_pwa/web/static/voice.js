/* herdr-pwa: what VoiceOver gets. A poll never speaks, moves focus or leaves a half-drawn
 * region; the page's one live region says what a write did and that a message arrived. */
(function (hp) {
  "use strict";

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

  hp.bus.on("swap:capture", function (detail) {
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
    writeTarget = hp.isRead(detail) ? "" : target.id;
  });

  // The newest message in the chat, if the newest thing in it is the agent speaking.
  function newestMessage() {
    var units = document.querySelectorAll("#agent-view.chat .turn");
    var last = units[units.length - 1];
    return last && last.classList.contains("turn--agent") ? last : null;
  }

  hp.bus.on("swapped:announce", function (detail) {
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
    if (!hp.isRead(detail) || !document.querySelector("#agent-view.chat")) {
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
  hp.bus.on("ready:announce", function () {
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
})(window.hp);
