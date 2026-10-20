/* herdr-pwa: Back belongs to the herd. Only the feed's own links push; every other move replaces
 * the entry it leaves, and a page leaves only once its event stream is closed, so it can be kept
 * in the back-forward cache. An agent page's entry restores no scroll. */
(function (hp) {
  "use strict";

  // WebKit's restored scroll reads as the reader's, and its replace keeps the entry: each page sets its own.
  if ("scrollRestoration" in window.history) {
    window.history.scrollRestoration = document.getElementById("agent-view") ? "manual" : "auto";
  }

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
  hp.bus.on("swap:admit", function (detail) {
    var xhr = detail.xhr;
    if (!xhr || !isSheet(detail) || xhr.status < 400) {
      return;
    }
    detail.shouldSwap = true;
    detail.isError = false;
    if (String(xhr.responseText).indexOf("<main") === -1) {
      detail.selectOverride = "unset";
    }
  });

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
    if (hp.stream.open()) {
      hp.stream.close();
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
      if (!hp.sendhold.sending()) {
        leave(link.href, true);
      }
    } else if (hp.stream.open()) {
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
})(window.hp);
