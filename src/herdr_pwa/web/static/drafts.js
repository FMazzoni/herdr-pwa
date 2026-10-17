/* herdr-pwa: a composer's draft survives navigation and the redraws around it. */
(function (hp) {
  "use strict";

  /* A composer's draft survives navigation: kept per session in this tab only,
   * restored as a value (never markup), dropped once sent or at a no-echo prompt. */
  var DRAFT = "hp-draft:";

  function keep(box) {
    var s = hp.storage("sessionStorage");
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
    var s = hp.storage("sessionStorage");
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
  hp.bus.on("swapped:restore", function (detail) {
    settle(detail.elt.parentNode || document);
  });
  hp.bus.on("ready:restore", function () {
    settle(document);
  });
})(window.hp);
