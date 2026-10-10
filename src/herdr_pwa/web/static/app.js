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

  /* A composer's draft survives navigation: kept per session in this tab only,
   * restored as a value (never markup), dropped once sent or at a no-echo prompt. */
  var DRAFT = "hp-draft:";

  function store() {
    try {
      return window.sessionStorage;
    } catch (err) {
      return null;
    }
  }

  function keep(box) {
    var s = store();
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

  function settle(root) {
    var boxes = root.querySelectorAll("textarea[data-hp-draft]");
    var s = store();
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

  /* Back and End are disabled while a write is in flight, or the result is never seen. */
  document.addEventListener("htmx:beforeRequest", function (event) {
    if (event.detail.requestConfig && event.detail.requestConfig.verb !== "get") {
      document.body.classList.add("is-sending");
    }
  });
  document.addEventListener("htmx:afterRequest", function () {
    document.body.classList.remove("is-sending");
  });

  document.addEventListener("visibilitychange", rearm);
  /* bfcache restore and iOS standalone resume skip visibilitychange. */
  window.addEventListener("pageshow", function (event) {
    if (event.persisted) {
      rearm();
    }
  });
})();
