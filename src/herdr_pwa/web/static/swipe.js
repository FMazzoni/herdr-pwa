/* herdr-pwa: a sheet with a grabber follows a finger down and closes, as its grabber promises.
 * The sheet is [data-hp-swipe] inside the details it closes; its grabber and header are
 * [data-hp-grab], where a finger always moves the sheet. In the list below them a finger scrolls
 * the list, unless the list is at its top and the finger pulls down. Only a transform moves, and
 * closing is the same details.open = false a tap makes, so whatever a tap-close undoes (an armed
 * Stop) a swipe undoes too. No history entry is made. */
(function (hp) {
  "use strict";

  // A click this soon after a drag is the drag's compat click; a tap is never read from a drag.
  var DRAGGED_CLICK_MS = 400;
  // The finger's speed is read over its last moves within this window.
  var SPEED_WINDOW_MS = 60;
  // A settle whose transitionend never comes (a hidden tab, an interrupted transition) ends here.
  var SETTLE_CEILING_MS = 600;

  var drag = null;
  var draggedAt = 0;
  var settling = false;
  // A redraw of the tray held back while a sheet was under a finger, fetched once it is let go.
  var heldRedraw = false;

  function stillness() {
    return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  }

  function place(body, dy) {
    body.style.transform = dy > 0 ? "translateY(" + dy + "px)" : "";
  }

  function speed(samples) {
    var last = samples[samples.length - 1];
    var first = last;
    for (var i = samples.length - 1; i >= 0 && last.t - samples[i].t <= SPEED_WINDOW_MS; i--) {
      first = samples[i];
    }
    return last.t > first.t ? (last.y - first.y) / (last.t - first.t) : 0;
  }

  function settled() {
    settling = false;
    if (heldRedraw) {
      heldRedraw = false;
      var tray = document.getElementById("agent-tray");
      if (tray && typeof window.htmx !== "undefined") {
        hp.refetch(tray);
      }
    }
  }

  // Close or spring back; under Reduce Motion at once, otherwise over the sheet's settle.
  function letGo(body, sheet, close) {
    body.classList.remove("is-dragging");
    var ended = false;
    function end() {
      if (ended) {
        return;
      }
      ended = true;
      body.classList.remove("is-settling");
      if (close) {
        sheet.open = false;
      }
      place(body, 0);
      settled();
    }
    if (stillness()) {
      end();
      return;
    }
    settling = true;
    body.classList.add("is-settling");
    body.style.transform = close ? "translateY(100%)" : "";
    body.addEventListener("transitionend", end, { once: true });
    window.setTimeout(end, SETTLE_CEILING_MS);
  }

  document.addEventListener("pointerdown", function (event) {
    drag = null;
    var target = event.target;
    var body = target && target.closest ? target.closest("[data-hp-swipe]") : null;
    var sheet = body ? body.closest("details") : null;
    if (!sheet || !sheet.open || settling || !event.isPrimary || event.button > 0) {
      return;
    }
    drag = {
      id: event.pointerId,
      body: body,
      sheet: sheet,
      handle: !!target.closest("[data-hp-grab]"),
      x: event.clientX,
      y: event.clientY,
      phase: "wait",
      from: 0,
      samples: [],
    };
  });

  function decide(event) {
    var dy = event.clientY - drag.y;
    drag.phase = hp.logic.swipeStart({
      dx: event.clientX - drag.x,
      dy: dy,
      handle: drag.handle,
      atTop: drag.body.scrollTop <= 0,
    });
    if (drag.phase === "drag") {
      drag.from = dy;
      drag.body.classList.add("is-dragging");
      try {
        drag.body.setPointerCapture(drag.id);
      } catch (err) {
        /* a pointer the browser does not know (sent by a test) cannot be captured */
      }
    }
  }

  document.addEventListener("pointermove", function (event) {
    if (!drag || event.pointerId !== drag.id) {
      return;
    }
    if (!drag.body.isConnected) {
      drag = null;
      return;
    }
    if (drag.phase === "wait") {
      decide(event);
    }
    if (drag.phase !== "drag") {
      return;
    }
    var dy = Math.max(0, event.clientY - drag.y - drag.from);
    drag.samples.push({ t: event.timeStamp, y: event.clientY });
    place(drag.body, dy);
  });

  function finish(event, cancelled) {
    if (!drag || event.pointerId !== drag.id) {
      return;
    }
    var was = drag;
    drag = null;
    if (was.phase !== "drag") {
      return;
    }
    draggedAt = Date.now();
    var dy = Math.max(0, event.clientY - was.y - was.from);
    var close = !cancelled && was.body.isConnected && hp.logic.swipeEnd({
      dy: dy,
      velocity: speed(was.samples),
      height: was.body.getBoundingClientRect().height,
    });
    letGo(was.body, was.sheet, close);
  }

  document.addEventListener("pointerup", function (event) {
    finish(event, false);
  });
  document.addEventListener("pointercancel", function (event) {
    finish(event, true);
  });

  // Whether this move may be the sheet's: claimed already, or a pull down from the list's top
  // still inside the slop, which iOS would otherwise start scrolling (or bouncing) first.
  function sheetsMove(event) {
    if (!drag || drag.phase === "let") {
      return false;
    }
    var touch = event.touches && event.touches[0];
    var down = !!touch && touch.clientY > drag.y;
    return drag.phase === "drag" || (down && drag.body.scrollTop <= 0);
  }

  // touch-action cannot say "pan up only", so the list's pull down is taken here instead.
  document.addEventListener("touchmove", function (event) {
    if (event.cancelable && sheetsMove(event)) {
      event.preventDefault();
    }
  }, { passive: false });

  // Ahead of every other click listener, so a drag that began on Stop… or a key never taps it.
  window.addEventListener("click", function (event) {
    if (Date.now() - draggedAt < DRAGGED_CLICK_MS && event.detail !== 0) {
      draggedAt = 0;
      event.preventDefault();
      event.stopImmediatePropagation();
    }
  }, true);

  // Nothing is redrawn under a finger: the tray's redraw waits until its sheet is let go.
  hp.bus.on("swap:veto", function (detail) {
    var target = detail.target;
    if (!target || target.id !== "agent-tray" || !hp.isRead(detail)) {
      return;
    }
    if ((drag && drag.phase === "drag") || settling) {
      detail.shouldSwap = false;
      heldRedraw = true;
    }
  });
})(window.hp);
