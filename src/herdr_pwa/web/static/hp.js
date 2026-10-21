/* herdr-pwa's client core, the first of its scripts (assets.SCRIPTS names them in order): what
 * the others share, under window.hp: the page's decisions and the bus that orders a swap. Every
 * one is a classic script, stamped and deferred; none builds markup from strings or evals. */
(function () {
  "use strict";

  /* ---- The page's decisions, apart from the page ----
   * What lies between the two markers reads only its arguments: it is handed what was measured
   * and says what to do. tests/js loads that stretch of this file alone, in a context that has no
   * page, no clock and no timers, so a reference to any of them there fails the tests. The rest
   * of the scripts measure and act. */
  // hp:logic:start
  var logic = (function () {
    // How close to the very bottom a reader's own scroll must come to pin the page again.
    var BOTTOM_PX = 2;
    // The stream's first retry, and the longest it waits between retries.
    var RETRY_FIRST_MS = 1000;
    var RETRY_MAX_MS = 30000;

    /* The pin, as { pinned, lastY, reader }: lastY is where the page was last left, by the reader
     * or by this script, and reader says whether this event was the reader's own scroll. Each
     * event carries what was measured as it happened: y, the scroll position, and gap, how far the
     * page still runs below the screen. */
    function pin(state, event) {
      var bottom = event.gap <= BOTTOM_PX;
      var next = { pinned: state.pinned, lastY: state.lastY, reader: false };
      switch (event.type) {
        // A scroll ending where this script left the page was this script's; any other is the
        // reader's, and pins the page at the very end and unpins it anywhere else.
        case "scroll":
          if (event.y !== state.lastY) {
            next = { pinned: bottom, lastY: event.y, reader: true };
          }
          return next;
        // This script moved the page (a hold, an outcome brought into view).
        case "moved":
          next.lastY = event.y;
          return next;
        // This script went to the end: pinned only if it got there, since a tall question's top
        // is not the end.
        case "landed":
          return { pinned: bottom, lastY: event.y, reader: false };
        // A swap claims the page only if it moved it: one that left the page where it stood
        // claims nothing, since the reader's last scroll may not have been heard yet.
        case "swapped":
          if (event.before === null || event.y !== event.before) {
            next.lastY = event.y;
          }
          return next;
        // A fold opened short of the end: the reader is reading the fold now.
        case "unfolded":
          next.pinned = state.pinned && bottom;
          return next;
        case "earlier":
          next.pinned = false;
          return next;
        // Sending, or the tap on "Question waiting": the reader is reading the end.
        case "sent":
        case "jumped":
          next.pinned = true;
          return next;
        default:
          throw new Error("no such pin event: " + event.type);
      }
    }

    /* Where landing at the end leaves the page: the end, unless a waiting question is taller than
     * the room between the bar and the dock; then the question's top, just under the bar. */
    function landing(page) {
      var end = page.scrollHeight - page.viewHeight;
      if (!page.asking || page.regionHeight <= page.dockTop - page.barBottom) {
        return end;
      }
      return Math.min(end, page.scrollY + page.askedTop - page.barBottom);
    }

    // One server-sent event block: its event name (message when it has none) and its data.
    function eventOf(block) {
      var found = { kind: "message", data: "" };
      block.split("\n").forEach(function (line) {
        if (line.indexOf("event:") === 0) {
          found.kind = line.slice(6).trim();
        } else if (line.indexOf("data:") === 0) {
          found.data = line.slice(5).trim();
        }
      });
      return found;
    }

    // The topic a block calls stale, or null: only "stale" is acted on, and its data is a topic.
    function staleTopic(block) {
      var heard = eventOf(block);
      return heard.kind === "stale" && heard.data ? heard.data : null;
    }

    // The server ending a stream at its lifetime, which loses nothing: the page just reconnects.
    function rotates(block) {
      return eventOf(block).kind === "rotate";
    }

    // An open page watched the agent finish when a redraw first shows it done: say so once.
    function finishedNow(before, after) {
      return after === "done" && before !== "done";
    }

    // Each whole block in what has arrived from `from` on, and where the unfinished rest begins.
    function blocks(text, from) {
      var whole = [];
      var cut = text.indexOf("\n\n", from);
      while (cut !== -1) {
        whole.push(text.slice(from, cut));
        from = cut + 2;
        cut = text.indexOf("\n\n", from);
      }
      return { blocks: whole, from: from };
    }

    // A lost stream waits `ms` before its next try, and twice that (up to the cap) after it.
    function backoff(ms) {
      return { wait: ms, next: Math.min(ms * 2, RETRY_MAX_MS) };
    }

    /* What to hold in place across a swap, from the units' boxes in page order: the first wholly
     * in view under the bar, else the first still on screen at all, as when one turn is taller
     * than the screen. Its index, or -1. */
    function markAt(boxes, under, viewHeight) {
      var partly = -1;
      for (var i = 0; i < boxes.length; i++) {
        if (boxes[i].bottom <= 0) {
          continue;
        }
        if (boxes[i].top >= under && boxes[i].top < viewHeight) {
          return i;
        }
        partly = partly === -1 ? i : partly;
      }
      return partly;
    }

    /* The same unit after the swap: the same key, nearest its old place, since both windows drop
     * their oldest as new ones arrive and the feed re-sorts its cards. Its index, or -1. */
    function nearest(keys, key, index) {
      var best = -1;
      for (var i = 0; i < keys.length; i++) {
        var nearer = best === -1 || Math.abs(i - index) < Math.abs(best - index);
        if (nearer && keys[i] === key) {
          best = i;
        }
      }
      return best;
    }

    /* A read's swap is put off while the reader is busy, for any region that fetches itself and
     * can be found again by its id. */
    function defers(region, busy) {
      return busy && region.polled && !!region.id;
    }

    // The no-reply notice stays up at least this long: past the answer region's 5s poll, so it
    // can be read before a redraw takes it away.
    var LOST_READ_MS = 8000;

    /* How much longer a read must wait before it may redraw the answer region, from how long its
     * no-reply notice has been up (-1: none is up). */
    function lostHold(shownFor) {
      return shownFor < 0 ? 0 : Math.max(0, LOST_READ_MS - shownFor);
    }

    /* The status bubble's motion styles, every one in this one place, picked by the bubble's
     * data-working-motion (web/chat_status.py's WORKING_MOTION names the default). A style is
     * how long one crest takes to cross the words (period, ms), how wide it is as a share of
     * them (reach), and how high it lifts a letter (rise, em); null is a style that never moves.
     * To add one, add an entry here and name it in WORKING_MOTION to wear it. */
    var MOTIONS = {
      // One gentle crest of colour crossing the words, nothing lifted.
      crest: { period: 1600, reach: 0.35, rise: 0 },
      // The same crest lifting each letter a little as it passes, as the first working line did.
      "crest-lift": { period: 1600, reach: 0.35, rise: 0.08 },
      // A slower, wider crest, for a calmer bubble.
      drift: { period: 2800, reach: 0.5, rise: 0 },
      still: null,
    };

    var MINUTE = 60;
    var HOUR = 3600;
    var DAY = 86400;

    /* A span in the words herdr_pwa.clock.elapsed gives it: 42s, 4m 12s, 2m, 1h 12m, 2d 7h. The
     * server words the first drawing; this only carries it on between redraws, and
     * tests/fixtures/elapsed.json holds both to the same table. */
    function elapsed(seconds) {
      var whole = Math.max(0, Math.floor(seconds));
      if (whole >= DAY) {
        return Math.floor(whole / DAY) + "d " + Math.floor((whole % DAY) / HOUR) + "h";
      }
      if (whole >= HOUR) {
        return Math.floor(whole / HOUR) + "h " + Math.floor((whole % HOUR) / MINUTE) + "m";
      }
      if (whole < MINUTE) {
        return whole + "s";
      }
      var rest = whole % MINUTE;
      return Math.floor(whole / MINUTE) + "m" + (rest ? " " + rest + "s" : "");
    }

    // A quiet stretch to its largest unit, as the server words it (web/chat_status.py quiet_words).
    function quietFor(seconds) {
      return "Quiet " + elapsed(Math.floor(seconds / MINUTE) * MINUTE).split(" ")[0];
    }

    /* Whether a bubble drawn `quiet` seconds after the log last moved has, `sinceMs` later, been
     * still past `after` seconds: nothing redraws a log that does not move, so this page says so. */
    function goneQuiet(quiet, sinceMs, after) {
      if (quiet === null || !(after > 0)) {
        return false;
      }
      return quiet + sinceMs / 1000 >= after;
    }

    // How wide the wave's crest is, as a share of the words it crosses, unless a motion says.
    var WAVE_REACH = 0.35;

    /* How far the wave lifts a glyph at `x` on a verb `width` wide, `phase` (0 to 1) of the way
     * through one sweep: 0 to 1, a soft crest that enters on the left and leaves on the right, so
     * the crest moves at one speed across a wide m and a narrow i alike. */
    function wave(x, width, phase, share) {
      var reach = Math.max(width * (share || WAVE_REACH), 1);
      var crest = -reach + phase * (width + 2 * reach);
      var off = Math.abs(x - crest) / reach;
      return off >= 1 ? 0 : (1 + Math.cos(Math.PI * off)) / 2;
    }

    /* Where a glyph centred at `x` stands across the crest, for the colour it wears: 0 at the
     * trailing edge, 1 at the leading edge, so the colours run through the crest as one gradient. */
    function glow(x, width, phase, share) {
      var reach = Math.max(width * (share || WAVE_REACH), 1);
      var crest = -reach + phase * (width + 2 * reach);
      return Math.min(1, Math.max(0, (x - crest) / (2 * reach) + 0.5));
    }

    // How far a finger moves before it is a drag or a scroll rather than a tap.
    var SWIPE_SLOP_PX = 8;
    // A sheet let go past this share of its height, or this far, closes (iOS sheets close near a third).
    var SWIPE_CLOSE_SHARE = 0.3;
    var SWIPE_CLOSE_MAX_PX = 120;
    // A flick: this fast at the end (px per ms), and further than a twitch.
    var FLICK_SPEED = 0.5;
    var FLICK_MIN_PX = 16;

    /* Whose a finger is once it leaves the slop, given how far it went (dx, dy, down positive),
     * whether it began on the sheet's grabber or header, and whether the list is at its top:
     * "drag" moves the sheet, "let" leaves it to the list, "wait" has not decided. */
    function swipeStart(move) {
      if (Math.abs(move.dx) < SWIPE_SLOP_PX && Math.abs(move.dy) < SWIPE_SLOP_PX) {
        return "wait";
      }
      if (move.handle) {
        return "drag";
      }
      var down = move.dy > 0 && Math.abs(move.dy) > Math.abs(move.dx);
      return down && move.atTop ? "drag" : "let";
    }

    // Whether a sheet let go `dy` px down, moving at `velocity` px per ms, closes or springs back.
    function swipeEnd(release) {
      if (release.velocity <= -FLICK_SPEED) {
        return false;
      }
      if (release.velocity >= FLICK_SPEED && release.dy >= FLICK_MIN_PX) {
        return true;
      }
      return release.dy >= Math.min(release.height * SWIPE_CLOSE_SHARE, SWIPE_CLOSE_MAX_PX);
    }

    return {
      RETRY_FIRST_MS: RETRY_FIRST_MS,
      pin: pin,
      landing: landing,
      eventOf: eventOf,
      staleTopic: staleTopic,
      rotates: rotates,
      finishedNow: finishedNow,
      blocks: blocks,
      backoff: backoff,
      markAt: markAt,
      nearest: nearest,
      defers: defers,
      LOST_READ_MS: LOST_READ_MS,
      lostHold: lostHold,
      MOTIONS: MOTIONS,
      elapsed: elapsed,
      quietFor: quietFor,
      goneQuiet: goneQuiet,
      wave: wave,
      glow: glow,
      swipeStart: swipeStart,
      swipeEnd: swipeEnd,
    };
  })();
  // hp:logic:end

  /* ---- A swap runs in named phases, not in the order its listeners were added ----
   * htmx tells each htmx:beforeSwap and htmx:afterSwap listener in turn, so with one listener per
   * concern it was the order of the code that decided, say, that a held-back read captures
   * nothing. Here each concern names its phase and the bus runs the phases in order:
   *   swap:admit     a failure htmx would drop is let through (before anything may veto it)
   *   swap:veto      a swap is held back (shouldSwap = false)
   *   swap:capture   what the swap will destroy is noted: the reader's place, folds, focus
   *   swapped:restore   what the new drawing lost is put back: drafts, folds, the stats sheet
   *   swapped:scroll    the page is moved where it belongs, and the pin told
   *   swapped:announce  what is said aloud, and what runs on what was drawn
   * The page's first drawing runs ready:restore, ready:scroll and ready:announce on
   * DOMContentLoaded. Handlers that share a phase must not depend on each other's order. */
  var PHASES = {
    swap: ["admit", "veto", "capture"],
    swapped: ["restore", "scroll", "announce"],
    ready: ["restore", "scroll", "announce"],
  };
  var handlers = {};
  Object.keys(PHASES).forEach(function (moment) {
    PHASES[moment].forEach(function (phase) {
      handlers[moment + ":" + phase] = [];
    });
  });

  // One handler's throw is reported as a listener's would be, and the others still run.
  function report(err) {
    if (typeof window.reportError === "function") {
      window.reportError(err);
    } else {
      window.setTimeout(function () {
        throw err;
      }, 0);
    }
  }

  function run(moment, detail) {
    PHASES[moment].forEach(function (phase) {
      handlers[moment + ":" + phase].forEach(function (handler) {
        try {
          handler(detail);
        } catch (err) {
          report(err);
        }
      });
    });
  }

  var bus = {
    // Run `handler` in the named phase, e.g. "swap:veto"; it is handed htmx's event.detail.
    on: function (name, handler) {
      if (!handlers[name]) {
        throw new Error("no such phase: " + name);
      }
      handlers[name].push(handler);
    },
  };

  document.addEventListener("htmx:beforeSwap", function (event) {
    run("swap", event.detail);
  });
  document.addEventListener("htmx:afterSwap", function (event) {
    run("swapped", event.detail);
  });
  document.addEventListener("DOMContentLoaded", function () {
    run("ready", null);
  });

  /* A region fetches itself as its own source, so its hx-sync rule applies: a key tap's request
   * replaces this one, and this one waits behind a key already out. With no source htmx uses the
   * body, and the redraw would land over the key's controls mid-send. */
  function refetch(node) {
    window.htmx.ajax("GET", node.getAttribute("data-hp-poll"), { source: node, target: node, swap: "outerHTML" });
  }

  // A read: a region fetching itself, never a write.
  function isRead(detail) {
    return !!(detail.requestConfig && detail.requestConfig.verb === "get");
  }

  // The tab's or the device's storage, or null where reading it at all throws.
  function storage(name) {
    try {
      return window[name];
    } catch (err) {
      return null;
    }
  }

  // A yes-or-no this device remembers, false wherever its storage cannot be read.
  function kept(key) {
    try {
      var device = storage("localStorage");
      return !!device && device.getItem(key) === "1";
    } catch (err) {
      return false;
    }
  }

  // Remembers a yes-or-no on this device; where storage is blocked it lasts this page only.
  function keep(key, on) {
    try {
      var device = storage("localStorage");
      if (device) {
        device.setItem(key, on ? "1" : "0");
      }
    } catch (err) {
      /* blocked storage: the caller's page still shows the choice. */
    }
  }

  window.hp = { logic: logic, bus: bus, refetch: refetch, isRead: isRead, storage: storage, kept: kept, keep: keep };
})();
