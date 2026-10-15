/* herdr-pwa: the only hand-written client script. It never builds markup from
 * strings and never evals. Regions are redrawn when the event stream says they
 * went stale; htmx trigger filters would need eval, so the server stops a hidden
 * page's polls (HTTP 286) and this script re-arms them. */
(function () {
  "use strict";

  /* ---- The page's decisions, apart from the page ----
   * What lies between the two markers reads only its arguments: it is handed what was measured
   * and says what to do. tests/js loads that stretch of this file alone, in a context that has no
   * page, no clock and no timers, so a reference to any of them there fails the tests. The rest
   * of this file measures and acts. */
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

    return {
      RETRY_FIRST_MS: RETRY_FIRST_MS,
      pin: pin,
      landing: landing,
      eventOf: eventOf,
      staleTopic: staleTopic,
      blocks: blocks,
      backoff: backoff,
      markAt: markAt,
      nearest: nearest,
      defers: defers,
    };
  })();
  // hp:logic:end

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

  /* A request still open when a navigation starts keeps this page out of the back-forward cache
   * (Firefox decides before pagehide), so an open event stream is closed first and the page
   * leaves a moment later. */
  var LEAVE_MS = 50;

  function leave(href, replace) {
    var go = function () {
      if (replace) {
        window.location.replace(href);
      } else {
        window.location.assign(href);
      }
    };
    if (stream) {
      closeStream();
      window.setTimeout(go, LEAVE_MS);
    } else {
      go();
    }
  }

  // A plain tap on a link to another page of this app, not a new tab, a download or a #jump.
  function pageLink(event) {
    var link = event.target && event.target.closest ? event.target.closest("a[href]") : null;
    if (!link || !plainClick(event) || link.target || link.hasAttribute("download")) {
      return null;
    }
    var here = window.location;
    if (link.origin !== here.origin || (link.pathname === here.pathname && link.search === here.search && link.hash)) {
      return null;
    }
    return link;
  }

  document.addEventListener("click", function (event) {
    var link = pageLink(event);
    if (!link || event.defaultPrevented) {
      return;
    }
    if (link.hasAttribute("data-hp-replace")) {
      event.preventDefault();
      /* Leaving mid-write would hide whether it landed. */
      if (!sending()) {
        leave(link.href, true);
      }
    } else if (stream) {
      event.preventDefault();
      leave(link.href, false);
    }
  });

  /* A finished write names its page; htmx's own redirect would push, so the page replaces. */
  document.addEventListener("hp:opened", function (event) {
    var where = event.detail && event.detail.where;
    if (typeof where === "string" && where.charAt(0) === "/" && where.charAt(1) !== "/") {
      leave(where, true);
    }
  });

  /* A region fetches itself as its own source, so its hx-sync rule applies: a key tap's request
   * replaces this one, and this one waits behind a key already out. With no source htmx uses the
   * body, and the redraw would land over the key's controls mid-send. */
  function refetch(node) {
    window.htmx.ajax("GET", node.getAttribute("data-hp-poll"), { source: node, target: node, swap: "outerHTML" });
  }

  /* Re-fetch every polling region once the page is visible again. */
  function rearm() {
    if (document.hidden || typeof window.htmx === "undefined") {
      return;
    }
    var nodes = document.querySelectorAll("[data-hp-poll]");
    for (var i = 0; i < nodes.length; i++) {
      refetch(nodes[i]);
    }
  }

  /* A composer's draft survives navigation: kept per session in this tab only,
   * restored as a value (never markup), dropped once sent or at a no-echo prompt. */
  var DRAFT = "hp-draft:";

  // The tab's or the device's storage, or null where reading it at all throws.
  function storage(name) {
    try {
      return window[name];
    } catch (err) {
      return null;
    }
  }

  function keep(box) {
    var s = storage("sessionStorage");
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

  function settle(within) {
    var boxes = within.querySelectorAll("textarea[data-hp-draft]");
    var s = storage("sessionStorage");
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

  /* Back and End are disabled while any write is in flight, or the result is never seen. Each
   * write is held by its own request, so a read finishing meanwhile releases nothing. */
  var writing = [];

  function sending() {
    return writing.length > 0;
  }

  function showSending() {
    document.body.classList.toggle("is-sending", sending());
  }

  // beforeSend, not beforeRequest: a request a listener cancels never sends, and never finishes.
  document.addEventListener("htmx:beforeSend", function (event) {
    var detail = event.detail;
    if (detail.requestConfig && detail.requestConfig.verb !== "get" && writing.indexOf(detail.xhr) === -1) {
      writing.push(detail.xhr);
      showSending();
    }
  });
  document.addEventListener("htmx:afterRequest", function (event) {
    var at = writing.indexOf(event.detail.xhr);
    if (at !== -1) {
      writing.splice(at, 1);
      showSending();
    }
  });

  /* ---- A tap on the answer region always says what became of it ----
   * The outcome banner is scrolled into view if the thumb left the region's top off screen,
   * and a tap that got no reply at all (a dropped link) shows the region's own notice. */
  function showOutcome(banner) {
    var top = banner.getBoundingClientRect().top;
    if (top < barBottom() || top > window.innerHeight * 0.6) {
      banner.scrollIntoView({ block: "start" });
      ownScroll();
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

  /* ---- Live updates: the server names a stale region, and the region fetches itself ----
   * One stream per page names the regions on it (data-hp-topic). An event carries only a
   * topic; the region then asks its own route, so every swap rule in this file still applies.
   * While the stream is down the regions poll slowly and the stream reconnects with backoff.
   * iOS drops a hidden page's connection, so the page closes it when hidden and opens it again
   * on return, with the one refresh rearm() makes. */
  var FALLBACK_MS = (Number(document.body.getAttribute("data-hp-fallback")) || 20) * 1000;
  var stream = null;
  var live = false;
  var retryMs = logic.RETRY_FIRST_MS;
  var retryTimer = 0;
  var fallbackTimer = 0;
  var missed = false;

  function topicNodes() {
    return document.querySelectorAll("[data-hp-topic]");
  }

  function streamTopics() {
    var nodes = topicNodes();
    var found = [];
    for (var i = 0; i < nodes.length; i++) {
      var topic = nodes[i].getAttribute("data-hp-topic");
      if (topic && found.indexOf(topic) === -1) {
        found.push(topic);
      }
    }
    return found;
  }

  // Each region named by the topic (or every region) fetches itself, once.
  function stale(topic) {
    if (typeof window.htmx === "undefined") {
      return;
    }
    var nodes = topicNodes();
    for (var i = 0; i < nodes.length; i++) {
      if (topic === null || nodes[i].getAttribute("data-hp-topic") === topic) {
        window.htmx.trigger(nodes[i], "hp:stale");
      }
    }
  }

  function polling(on) {
    if (on && !fallbackTimer) {
      fallbackTimer = window.setInterval(function () {
        if (!document.hidden && !live) {
          stale(null);
        }
      }, FALLBACK_MS);
    } else if (!on && fallbackTimer) {
      window.clearInterval(fallbackTimer);
      fallbackTimer = 0;
    }
  }

  function closeStream() {
    var mine = stream;
    // Cleared first: aborting fires loadend, which must not read as a lost stream.
    stream = null;
    live = false;
    if (mine) {
      mine.abort();
    }
  }

  function streamOpened() {
    live = true;
    retryMs = logic.RETRY_FIRST_MS;
    polling(false);
    // Whatever changed while the stream was down was told to nobody.
    if (missed) {
      missed = false;
      stale(null);
    }
  }

  function lostStream(mine) {
    if (stream !== mine) {
      return;
    }
    stream = null;
    live = false;
    missed = true;
    polling(true);
    if (!document.hidden) {
      var retry = logic.backoff(retryMs);
      retryTimer = window.setTimeout(connect, retry.wait);
      retryMs = retry.next;
    }
  }

  // Each whole event block in what has arrived; the rest waits for the next progress.
  function heardSoFar(text, from) {
    var arrived = logic.blocks(text, from);
    arrived.blocks.forEach(function (block) {
      var topic = logic.staleTopic(block);
      if (topic) {
        stale(topic);
      }
    });
    return arrived.from;
  }

  /* The stream is an XMLHttpRequest read as it arrives: no EventSource reconnecting on its own
   * behind the backoff, and abort() ends it at once when the page leaves (see leave()). */
  function connect() {
    window.clearTimeout(retryTimer);
    retryTimer = 0;
    if (stream || document.hidden) {
      return;
    }
    var topics = streamTopics();
    if (!topics.length) {
      return;
    }
    var query = topics.map(function (topic) {
      return "topic=" + encodeURIComponent(topic);
    }).join("&");
    var xhr = new XMLHttpRequest();
    var mine = { abort: function () { xhr.abort(); } };
    var read = 0;
    stream = mine;
    xhr.open("GET", "/events?" + query);
    xhr.setRequestHeader("Accept", "text/event-stream");
    xhr.onreadystatechange = function () {
      if (stream === mine && xhr.readyState >= 2 && xhr.status === 200 && !live) {
        streamOpened();
      }
    };
    xhr.onprogress = function () {
      if (stream === mine && xhr.status === 200) {
        read = heardSoFar(xhr.responseText, read);
      }
    };
    xhr.onloadend = function () {
      lostStream(mine);
    };
    xhr.send();
  }

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      closeStream();
      return;
    }
    // rearm() is the one refresh on return; the reopened stream owes none.
    missed = false;
    rearm();
    connect();
    markSeen();
  });
  document.addEventListener("DOMContentLoaded", connect);
  window.addEventListener("pagehide", closeStream);

  /* bfcache restore and iOS standalone resume skip visibilitychange. The restored
   * DOM is whatever was on screen when the page was left, so it says so until the
   * re-fetch replaces it. */
  window.addEventListener("pageshow", function (event) {
    if (event.persisted) {
      var nodes = document.querySelectorAll("[data-hp-poll]");
      for (var i = 0; i < nodes.length; i++) {
        nodes[i].classList.add("is-stale");
      }
      writing = [];
      showSending();
      missed = false;
      rearm();
      connect();
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
    // A pinned reader stays at the end as the keyboard opens or the dock grows.
    var wasEnd = moved && pin.pinned;
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
      if (Date.now() - lastTouch < 1500 || sending()) {
        disarmLater(node, 1.5);
        return;
      }
      refetch(region);
    }, seconds * 1000);
  }

  function armTimers(within) {
    var nodes = within.querySelectorAll ? within.querySelectorAll("[data-hp-disarm]") : [];
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
   * so it keeps its key as calls are added to it. The keys belong to one redraw of the view: any
   * other region's swap leaves the folds alone, so one the reader has closed since stays closed. */
  var unfolded = [];
  // Every disclosure the view had before its redraw, open or not.
  var drawn = [];

  function isView(detail) {
    return !!(detail.target && detail.target.id === "agent-view");
  }

  function outerDetails(details) {
    return details.parentElement ? details.parentElement.closest("details") : null;
  }

  function detailsKey(details) {
    var unit = details.closest(".turn");
    var inside = unit ? unit.querySelectorAll("details") : [];
    var own = details.getAttribute("data-hp-key");
    return (unit ? unitKey(unit) : "") + "#" + (own || Array.prototype.indexOf.call(inside, details));
  }

  document.addEventListener("htmx:beforeSwap", function (event) {
    if (!isView(event.detail)) {
      return;
    }
    unfolded = [];
    drawn = [];
    var all = event.detail.target.querySelectorAll("details");
    for (var i = 0; i < all.length; i++) {
      drawn.push(detailsKey(all[i]));
      if (all[i].open) {
        unfolded.push(detailsKey(all[i]));
      }
    }
  });

  document.addEventListener("htmx:afterSwap", function (event) {
    if (!isView(event.detail) || !unfolded.length) {
      return;
    }
    var keys = unfolded;
    var was = drawn;
    unfolded = [];
    drawn = [];
    var all = document.querySelectorAll("#agent-view details");
    for (var i = 0; i < all.length; i++) {
      if (keys.indexOf(detailsKey(all[i])) === -1) {
        continue;
      }
      all[i].open = true;
      /* A lone call that became the first of a run is now inside a fold that was not there
       * before: open that too. A fold that was there and closed was closed by the reader, with
       * a call inside it left open, and it stays closed. */
      for (var d = outerDetails(all[i]); d && was.indexOf(detailsKey(d)) === -1; d = outerDetails(d)) {
        d.open = true;
      }
    }
  });

  /* ---- Chat and Pane open on their newest line and follow it only while the reader is pinned ----
   * Both read oldest first, so the end of the page is the newest turn or terminal line, just
   * above the dock, and a question the agent asks sits after that, where the thumb is. Pinned is
   * a state, not a position: set on landing at the end, cleared by a scroll of the reader's that
   * leaves the end, and set again only when the reader reaches the very end themselves or sends.
   * A pinned page follows the end; any other keeps what the reader is reading where it was. */
  // A redraw waits until the reader's scroll has been still this long, and no finger is down.
  var IDLE_MS = 250;
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
    pin = logic.pin(pin, event);
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
  function endY() {
    var region = document.getElementById("answer");
    var bar = document.getElementById("composer");
    var waiting = !!region && asking();
    return logic.landing({
      scrollHeight: root.scrollHeight,
      viewHeight: window.innerHeight,
      scrollY: window.scrollY,
      asking: waiting,
      regionHeight: waiting ? region.getBoundingClientRect().height : 0,
      askedTop: waiting ? askedIn(region).getBoundingClientRect().top : 0,
      dockTop: bar ? bar.getBoundingClientRect().top : window.innerHeight,
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
    var at = logic.markAt(boxes, barBottom(), window.innerHeight);
    return at === -1 ? null : { key: unitKey(units[at]), index: at, top: boxes[at].top, anchors: anchors };
  }

  /* The same unit after the swap: same key, nearest its old place, since both windows drop
   * their oldest as new ones arrive and the feed re-sorts its cards. */
  function hold(held) {
    var units = document.querySelectorAll(held.anchors || ANCHORS);
    var best = logic.nearest(Array.prototype.map.call(units, unitKey), held.key, held.index);
    if (best !== -1) {
      window.scrollBy(0, units[best].getBoundingClientRect().top - held.top);
      ownScroll();
    }
  }

  /* Only the reader moves the pin: a scroll this script did not make pins the page if it ends at
   * the very end and unpins it anywhere else, down as well as up, since a reader who opened a
   * tool's output scrolls down into it. A page that shrank under a pinned reader stays pinned. */
  window.addEventListener("scroll", function () {
    pinOn("scroll");
    if (pin.reader) {
      readerScrolledAt = Date.now();
    }
  }, { passive: true });

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
      if (readerBusy() || sending()) {
        redrawWhenStill();
        return;
      }
      var ids = deferred;
      deferred = [];
      ids.forEach(function (id) {
        var node = document.getElementById(id);
        if (node && typeof window.htmx !== "undefined") {
          refetch(node);
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
    if (!target || !logic.defers({ id: target.id, polled: target.hasAttribute("data-hp-poll") }, readerBusy())) {
      return false;
    }
    detail.shouldSwap = false;
    if (deferred.indexOf(target.id) === -1) {
      deferred.push(target.id);
    }
    redrawWhenStill();
    return true;
  }

  function isRead(detail) {
    return !!(detail.requestConfig && detail.requestConfig.verb === "get");
  }

  // An earlier page lands above the reader: hold a message, never follow the end.
  var EARLIER_ANCHORS = "#agent-view article.turn[data-hp-key]";

  function isEarlier(detail) {
    var elt = detail.requestConfig && detail.requestConfig.elt;
    return !!(elt && elt.hasAttribute && elt.hasAttribute("data-hp-earlier"));
  }

  document.addEventListener("htmx:beforeSwap", function (event) {
    if (!isRead(event.detail)) {
      return;
    }
    if (isEarlier(event.detail)) {
      pinOn("earlier");
      swapEnd = false;
      swapMark = mark(EARLIER_ANCHORS) || mark();
      return;
    }
    if (deferring(event.detail)) {
      return;
    }
    // Only the agent view follows its end; the feed is read from the top.
    swapEnd = !!document.getElementById("agent-view") && pin.pinned;
    swapMark = swapEnd ? null : mark();
  });

  // Where the page stood as a swap began, so the swap is blamed only for a move it made.
  var swapY = null;
  document.addEventListener("htmx:beforeSwap", function (event) {
    swapY = event.detail.shouldSwap ? window.scrollY : null;
  });

  document.addEventListener("htmx:afterSwap", function (event) {
    if (isRead(event.detail)) {
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
    var bar = document.getElementById("composer");
    var dockTop = bar ? bar.getBoundingClientRect().top : window.innerHeight;
    // Waiting below until the question itself (else the region's first line) is wholly in view.
    return askedIn(region).getBoundingClientRect().bottom > dockTop;
  }

  function showJump() {
    if (!jump) {
      return;
    }
    var below = questionBelow();
    jump.hidden = !below;
    var question = below ? document.querySelector("#answer .answer__question") : null;
    var key = question ? words(question) : below ? "waiting" : "";
    if (below && key !== jumpSaid) {
      say("A question is waiting at the end.");
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
      var device = storage("localStorage");
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
        var device = storage("localStorage");
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

  function say(text) {
    var spoken = document.getElementById("hp-live");
    if (!spoken || !text) {
      return;
    }
    // Emptied first, so the same words twice are still news.
    spoken.textContent = "";
    window.setTimeout(function () {
      spoken.textContent = text;
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

  /* ---- The stats strip keeps its details open across the redraws that refresh it ---- */
  var statsOpen = false;

  document.addEventListener("htmx:beforeSwap", function (event) {
    var target = event.detail.target;
    if (target && target.id === "agent-stats") {
      var sheet = target.querySelector("details");
      statsOpen = !!(sheet && sheet.open);
    }
  });

  document.addEventListener("htmx:afterSwap", function () {
    var sheet = statsOpen ? document.querySelector("#agent-stats details") : null;
    statsOpen = false;
    if (sheet) {
      sheet.open = true;
    }
  });

  /* ---- Seen: a finish the phone has shown is not news ----
   * Said on load and again each time the page comes back into view (the visibility listener). */

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
})();
