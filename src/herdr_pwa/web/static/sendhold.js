/* herdr-pwa: a write's outcome is never lost. While a write is out the page says so (is-sending)
 * and Back and End wait; its outcome is brought into view; a tap that got no reply shows the
 * region's own notice, and no read redraws that notice away before it can be read. */
(function (hp) {
  "use strict";

  /* Back and End are disabled while any write is in flight, or the result is never seen. Each
   * write is held by its own request, so a read finishing meanwhile releases nothing. */
  var writing = [];

  function sending() {
    return writing.length > 0;
  }

  function showSending() {
    document.body.classList.toggle("is-sending", sending());
  }

  /* Whether a write is out (Back, End and a held redraw wait on it), and a page restored from the
   * back-forward cache forgetting the writes it had out when it was left. */
  hp.sendhold = {
    sending: sending,
    reset: function () {
      writing = [];
      showSending();
    },
  };

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
    if (top < hp.chatend.barBottom() || top > window.innerHeight * 0.6) {
      banner.scrollIntoView({ block: "start" });
      hp.chatend.ownScroll();
    }
  }

  hp.bus.on("swapped:scroll", function (detail) {
    var config = detail.requestConfig;
    var region = document.getElementById("answer");
    if (!region || !config || config.verb === "get") {
      return;
    }
    var banner = region.querySelector(".answer > .banner:not([hidden])");
    if (banner) {
      showOutcome(banner);
    }
  });

  // The no-reply notice showing now, and when it came up.
  var lostShown = { notice: null, at: 0 };
  var lostTimer = 0;

  function lost(event) {
    var elt = event.detail && event.detail.elt;
    var region = elt && elt.closest ? elt.closest("#answer") : null;
    var notice = region ? region.querySelector("[data-hp-lost]") : null;
    if (notice) {
      if (notice.hidden) {
        lostShown = { notice: notice, at: Date.now() };
      }
      notice.hidden = false;
      showOutcome(notice);
    }
  }
  document.addEventListener("htmx:sendError", lost);
  document.addEventListener("htmx:timeout", lost);
  document.addEventListener("htmx:responseError", lost);

  /* A read that would redraw the notice away before it could be read is dropped, and the region
   * fetched afresh once it has been up long enough. A tap's own answer is never held. */
  hp.bus.on("swap:veto", function (detail) {
    var target = detail.target;
    if (!target || target.id !== "answer" || !hp.isRead(detail)) {
      return;
    }
    var notice = lostShown.notice;
    var up = !!notice && notice.isConnected && !notice.hidden;
    var wait = hp.logic.lostHold(up ? Date.now() - lostShown.at : -1);
    if (wait <= 0) {
      return;
    }
    detail.shouldSwap = false;
    window.clearTimeout(lostTimer);
    lostTimer = window.setTimeout(function () {
      if (target.isConnected && typeof window.htmx !== "undefined") {
        hp.refetch(target);
      }
    }, wait);
  });
})(window.hp);
