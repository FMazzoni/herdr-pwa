/* herdr-pwa: where the Chat and Pane tabs leave the reader. The pin decides when this script may
 * move the page (hp.logic.pin): a pinned reader follows the end, any other keeps the line they are
 * reading where it was, and no redraw lands under a finger or a running scroll. */
(function (hp) {
  "use strict";

  /* ---- Chat and Pane open on their newest line and follow it only while the reader is pinned ----
   * Both read oldest first, so the end of the page is the newest turn or terminal line, just
   * above the dock, and a question the agent asks sits after that, where the thumb is. Pinned is
   * a state, not a position: set on landing at the end, cleared by a scroll of the reader's that
   * leaves the end, and set again only when the reader reaches the very end themselves or sends.
   * A pinned page follows the end; any other keeps what the reader is reading where it was. */
  // A redraw waits until the reader's scroll has been still this long, and no finger is down.
  var IDLE_MS = 250;
  var root = document.documentElement;
  var pin = { pinned: true, lastY: window.scrollY, reader: false };
  var swapEnd = false;
  var swapMark = null;
  var touching = false;
  var readerScrolledAt = 0;
  var deferred = [];
  var idleTimer = 0;

  // A chat turn, or a line of the terminal mirror.
  var READ_UNITS = "#agent-view .turn, #agent-view > .pane > .pane__line";
  // What a poll may not move under the reader: those, and the feed's cards, which re-sort.
  var ANCHORS = READ_UNITS + ", #feed-list .card";

  function readUnits() {
    return document.querySelectorAll(READ_UNITS);
  }

  // How far the page still runs below the screen.
  function gap() {
    return root.scrollHeight - window.innerHeight - window.scrollY;
  }

  // The pin after `event`, with where the page is now and how far it runs on below.
  function pinOn(type, extra) {
    var event = { type: type, y: window.scrollY, gap: gap() };
    for (var name in extra) {
      event[name] = extra[name];
    }
    pin = hp.logic.pin(pin, event);
  }

  // This script moved the page: the scroll event that follows is not the reader's.
  function ownScroll() {
    pinOn("moved");
  }

  // The question itself, else its first option (a tappable one is an .option too), else the region.
  function askedIn(region) {
    return region.querySelector(".answer__question, .option") || region;
  }

  function asking() {
    var region = document.getElementById("answer");
    // A failure standing in for the region under its id is not a question.
    return !!(region && region.classList.contains("answer") && !region.classList.contains("answer--quiet"));
  }

  /* The end, unless a waiting question is taller than the room between the bar and the dock:
   * then its top, just under the bar, so the question and its first options show and Interrupt
   * waits below. The bar is measured, since what it holds varies by page. */
  // The top of what is fixed at the bottom: the status row over the composer, else the composer.
  function dockTop() {
    var bar = document.getElementById("agent-tray") || document.getElementById("composer");
    return bar ? bar.getBoundingClientRect().top : window.innerHeight;
  }

  function endY() {
    var region = document.getElementById("answer");
    var waiting = !!region && asking();
    return hp.logic.landing({
      scrollHeight: root.scrollHeight,
      viewHeight: window.innerHeight,
      scrollY: window.scrollY,
      asking: waiting,
      regionHeight: waiting ? region.getBoundingClientRect().height : 0,
      askedTop: waiting ? askedIn(region).getBoundingClientRect().top : 0,
      dockTop: dockTop(),
      barBottom: barBottom(),
    });
  }

  // Pinned only if this actually reached the end; a tall question's top is not the end.
  function toEnd() {
    if (readUnits().length) {
      window.scrollTo(0, endY());
      pinOn("landed");
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
  function mark(anchors) {
    var units = document.querySelectorAll(anchors || ANCHORS);
    var boxes = Array.prototype.map.call(units, function (unit) {
      return unit.getBoundingClientRect();
    });
    var at = hp.logic.markAt(boxes, barBottom(), window.innerHeight);
    return at === -1 ? null : { key: unitKey(units[at]), index: at, top: boxes[at].top, anchors: anchors };
  }

  /* The same unit after the swap: same key, nearest its old place, since both windows drop
   * their oldest as new ones arrive and the feed re-sorts its cards. */
  function hold(held) {
    var units = document.querySelectorAll(held.anchors || ANCHORS);
    var best = hp.logic.nearest(Array.prototype.map.call(units, unitKey), held.key, held.index);
    if (best !== -1) {
      window.scrollBy(0, units[best].getBoundingClientRect().top - held.top);
      ownScroll();
    }
  }

  /* Only the reader moves the pin: a scroll this script did not make pins the page if it ends at
   * the very end and unpins it anywhere else, down as well as up, since a reader who opened a
   * tool's output scrolls down into it. A page that shrank under a pinned reader stays pinned. */
  function hearScroll() {
    pinOn("scroll");
    if (pin.reader) {
      readerScrolledAt = Date.now();
    }
  }
  window.addEventListener("scroll", hearScroll, { passive: true });

  // Opening a fold at the end pushes the end away: the reader is reading the fold now.
  document.addEventListener("toggle", function (event) {
    var details = event.target;
    if (details.open && details.closest && details.closest("#agent-view")) {
      pinOn("unfolded");
    }
  }, true);

  // This listener is also what lets iOS Safari apply :active, which the press states in app.css need.
  document.addEventListener("touchstart", function () {
    touching = true;
  }, { capture: true, passive: true });
  ["touchend", "touchcancel"].forEach(function (name) {
    document.addEventListener(name, function (event) {
      touching = event.touches.length > 0;
      // Momentum runs on after the finger lifts; its scroll events keep the redraw waiting.
      readerScrolledAt = Date.now();
    }, { capture: true, passive: true });
  });

  function readerBusy() {
    return touching || Date.now() - readerScrolledAt < IDLE_MS;
  }

  // Each region whose redraw waited is fetched afresh once the reader is still.
  function redrawWhenStill() {
    window.clearTimeout(idleTimer);
    idleTimer = window.setTimeout(function () {
      idleTimer = 0;
      // A redraw during a write would draw live controls under the answer still on its way.
      if (readerBusy() || hp.sendhold.sending()) {
        redrawWhenStill();
        return;
      }
      var ids = deferred;
      deferred = [];
      ids.forEach(function (id) {
        var node = document.getElementById(id);
        if (node && typeof window.htmx !== "undefined") {
          hp.refetch(node);
        }
      });
    }, IDLE_MS);
  }

  /* Never redraw under a finger or a running scroll: a programmatic scroll then fights the
   * reader's (iOS drops momentum). That holds for every region that redraws itself, the stats
   * strip and the feed as much as the view, since each swap ends in a move to the end or a hold.
   * The answer is dropped and the region fetched afresh, by its id, once still. */
  function deferring(detail) {
    var target = detail.target;
    if (!target || !hp.logic.defers({ id: target.id, polled: target.hasAttribute("data-hp-poll") }, readerBusy())) {
      return false;
    }
    detail.shouldSwap = false;
    if (deferred.indexOf(target.id) === -1) {
      deferred.push(target.id);
    }
    redrawWhenStill();
    return true;
  }

  /* A write's answer is newer than a read its region put off, and draws what the write did;
   * the read, fetched after it, would redraw that outcome away. */
  function superseded(detail) {
    var target = detail.target;
    var at = target && detail.shouldSwap ? deferred.indexOf(target.id) : -1;
    if (at !== -1) {
      deferred.splice(at, 1);
    }
  }

  // An earlier page lands above the reader: hold a message, never follow the end.
  var EARLIER_ANCHORS = "#agent-view article.turn[data-hp-key]";

  function isEarlier(detail) {
    var elt = detail.requestConfig && detail.requestConfig.elt;
    return !!(elt && elt.hasAttribute && elt.hasAttribute("data-hp-earlier"));
  }

  /* A scroll is told of on the next frame, and a read can land before it: the reader who has
   * just left the end would still read as pinned and be taken back, or read as still. Heard
   * first, so whether the reader is busy is known when the read is put off or let through. An
   * earlier page is never put off. */
  hp.bus.on("swap:veto", function (detail) {
    if (!hp.isRead(detail)) {
      return;
    }
    hearScroll();
    if (!isEarlier(detail)) {
      deferring(detail);
    }
  });

  hp.bus.on("swap:capture", function (detail) {
    if (!hp.isRead(detail)) {
      superseded(detail);
      return;
    }
    if (isEarlier(detail)) {
      pinOn("earlier");
      swapEnd = false;
      swapMark = mark(EARLIER_ANCHORS) || mark();
      return;
    }
    // A read held back draws nothing, so there is nothing to follow or hold.
    if (!detail.shouldSwap) {
      return;
    }
    // Only the agent view follows its end; the feed is read from the top.
    swapEnd = !!document.getElementById("agent-view") && pin.pinned;
    swapMark = swapEnd ? null : mark();
  });

  // Where the page stood as a swap began, so the swap is blamed only for a move it made.
  var swapY = null;
  hp.bus.on("swap:capture", function (detail) {
    swapY = detail.shouldSwap ? window.scrollY : null;
  });

  hp.bus.on("swapped:scroll", function (detail) {
    if (hp.isRead(detail)) {
      if (swapEnd) {
        toEnd();
      } else if (swapMark) {
        hold(swapMark);
      }
      swapMark = null;
    }
    // A swap that shortened the page moved it too, and that move was not the reader's.
    pinOn("swapped", { before: swapY });
    swapY = null;
  });

  /* What the rest of the page asks of the pin: whether the reader is at the end and the way back
   * to it (the dock), the bar's bottom and a move of this script's own (the sent banner), and
   * what a turn is known by (folds, VoiceOver). */
  hp.chatend = {
    pinned: function () {
      return pin.pinned;
    },
    toEnd: toEnd,
    barBottom: barBottom,
    ownScroll: ownScroll,
    unitKey: unitKey,
  };

  /* ---- "Question waiting" floats above the dock while the question is below the reader ----
   * Shown only when the reader has scrolled away from a question still off screen; a tap takes
   * them back to the end and pins it. Said once per question, never on every scroll. */
  var jump = document.getElementById("hp-jump");
  var jumpSaid = "";

  function questionBelow() {
    var region = document.getElementById("answer");
    if (pin.pinned || !asking() || region.querySelector(".banner[data-hp-sent]")) {
      return false;
    }
    // Waiting below until the question itself (else the region's first line) is wholly in view.
    return askedIn(region).getBoundingClientRect().bottom > dockTop();
  }

  function showJump() {
    if (!jump) {
      return;
    }
    var below = questionBelow();
    jump.hidden = !below;
    var question = below ? document.querySelector("#answer .answer__question") : null;
    var key = question ? hp.voice.words(question) : below ? "waiting" : "";
    if (below && key !== jumpSaid) {
      hp.voice.say("A question is waiting at the end.");
    }
    if (below) {
      jumpSaid = key;
    }
  }

  if (jump) {
    window.addEventListener("scroll", showJump, { passive: true });
    document.addEventListener("htmx:afterSettle", showJump);
    jump.addEventListener("click", function () {
      pinOn("jumped");
      toEnd();
      showJump();
      var first = document.querySelector("#answer button, #answer a[href]");
      if (first) {
        first.focus({ preventScroll: true });
      }
    });
  }

  /* Sending is reading the end: the reply lands there. */
  document.addEventListener("htmx:beforeRequest", function (event) {
    var elt = event.detail.elt;
    if (elt && elt.id === "composer") {
      pinOn("sent");
      toEnd();
    }
  });
})(window.hp);
