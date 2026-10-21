/* herdr-pwa: live updates. The event stream names a stale region and the region fetches itself;
 * while the stream is down the regions poll slowly, and a hidden page closes it and asks for no
 * redraws until it is seen again. */
(function (hp) {
  "use strict";

  /* Every request from a hidden page says so; its poll answers 286 and stops. */
  document.addEventListener("htmx:configRequest", function (event) {
    if (document.hidden) {
      event.detail.headers["HP-Hidden"] = "1";
    }
  });

  /* Re-fetch every polling region once the page is visible again. */
  function rearm() {
    if (document.hidden || typeof window.htmx === "undefined") {
      return;
    }
    var nodes = document.querySelectorAll("[data-hp-poll]");
    for (var i = 0; i < nodes.length; i++) {
      hp.refetch(nodes[i]);
    }
  }

  /* ---- Live updates: the server names a stale region, and the region fetches itself ----
   * One stream per page names the regions on it (data-hp-topic). An event carries only a
   * topic; the region then asks its own route, so every swap rule in this file still applies.
   * While the stream is down the regions poll slowly and the stream reconnects with backoff.
   * iOS drops a hidden page's connection, so the page closes it when hidden and opens it again
   * on return, with the one refresh rearm() makes. */
  var FALLBACK_MS = (Number(document.body.getAttribute("data-hp-fallback")) || 20) * 1000;
  var stream = null;
  var live = false;
  var retryMs = hp.logic.RETRY_FIRST_MS;
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

  /* Whether the page's stream is open, and closing it: a page leaving closes it first. */
  hp.stream = {
    open: function () {
      return !!stream;
    },
    close: closeStream,
  };

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
    retryMs = hp.logic.RETRY_FIRST_MS;
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
    // A rotation at the stream's lifetime told the page everything up to its end.
    missed = missed || !mine.rotated;
    polling(true);
    if (document.hidden) {
      return;
    }
    if (mine.rotated) {
      retryTimer = window.setTimeout(connect, 0);
      return;
    }
    var retry = hp.logic.backoff(retryMs);
    retryTimer = window.setTimeout(connect, retry.wait);
    retryMs = retry.next;
  }

  // Each whole event block in what has arrived; the rest waits for the next progress.
  function heardSoFar(mine, text, from) {
    var arrived = hp.logic.blocks(text, from);
    arrived.blocks.forEach(function (block) {
      var topic = hp.logic.staleTopic(block);
      if (topic) {
        stale(topic);
      } else if (hp.logic.rotates(block)) {
        mine.rotated = true;
      }
    });
    return arrived.from;
  }

  /* The stream is an XMLHttpRequest read as it arrives: no EventSource reconnecting on its own
   * behind the backoff, and abort() ends it at once when the page leaves (backnav.js, leave()). */
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
    var mine = { rotated: false, abort: function () { xhr.abort(); } };
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
        read = heardSoFar(mine, xhr.responseText, read);
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
  });
  document.addEventListener("DOMContentLoaded", connect);
  window.addEventListener("pagehide", closeStream);

  /* ---- The phone losing the box ----
   * A read that gets no answer leaves its region showing what it drew last, which would read as
   * live. The region is marked stale and the bar says when the box was last heard from; the next
   * read that lands takes both away. */
  var heardAt = new Date();

  function clock(at) {
    return ("0" + at.getHours()).slice(-2) + ":" + ("0" + at.getMinutes()).slice(-2);
  }

  function readRegion(detail) {
    var elt = detail && detail.elt;
    return hp.isRead(detail) && elt && elt.closest ? elt.closest("[data-hp-poll]") : null;
  }

  function unheard(event) {
    var region = readRegion(event.detail);
    var bar = document.getElementById("hp-unreachable");
    if (!region || !bar) {
      return;
    }
    region.classList.add("is-stale");
    region.setAttribute("data-hp-unheard", "");
    document.getElementById("hp-unreachable-at").textContent = clock(heardAt);
    bar.hidden = false;
  }
  document.addEventListener("htmx:sendError", unheard);
  document.addEventListener("htmx:timeout", unheard);
  // The app's own failure screens are admitted as swaps (app.js); what is left came from no app.
  document.addEventListener("htmx:responseError", unheard);

  // Before the swap: afterRequest fires on the element an outerHTML swap has just detached.
  hp.bus.on("swap:capture", function (detail) {
    if (detail.isError || !readRegion(detail)) {
      return;
    }
    heardAt = new Date();
    var marked = document.querySelectorAll("[data-hp-unheard]");
    for (var i = 0; i < marked.length; i++) {
      marked[i].classList.remove("is-stale");
      marked[i].removeAttribute("data-hp-unheard");
    }
    var bar = document.getElementById("hp-unreachable");
    if (bar) {
      bar.hidden = true;
    }
  });

  /* bfcache restore and iOS standalone resume skip visibilitychange. The restored
   * DOM is whatever was on screen when the page was left, so it says so until the
   * re-fetch replaces it. */
  window.addEventListener("pageshow", function (event) {
    if (event.persisted) {
      var nodes = document.querySelectorAll("[data-hp-poll]");
      for (var i = 0; i < nodes.length; i++) {
        nodes[i].classList.add("is-stale");
      }
      hp.sendhold.reset();
      missed = false;
      rearm();
      connect();
    }
  });
})(window.hp);
