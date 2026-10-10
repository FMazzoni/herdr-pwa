/* herdr-pwa: the only hand-written client script. It never builds markup from
 * strings and never evals; htmx trigger filters would need eval, so the server
 * stops a hidden page's polls (HTTP 286) and this script re-arms them. */
(function () {
  "use strict";

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
  });

  /* Re-fetch every polling region once the page is visible again. */
  function rearm() {
    if (document.hidden || typeof window.htmx === "undefined") {
      return;
    }
    var nodes = document.querySelectorAll("[data-hp-poll]");
    for (var i = 0; i < nodes.length; i++) {
      window.htmx.ajax("GET", nodes[i].getAttribute("data-hp-poll"), {
        target: nodes[i],
        swap: "outerHTML",
      });
    }
  }

  document.addEventListener("visibilitychange", rearm);
  /* bfcache restore and iOS standalone resume skip visibilitychange. */
  window.addEventListener("pageshow", function (event) {
    if (event.persisted) {
      rearm();
    }
  });
})();
