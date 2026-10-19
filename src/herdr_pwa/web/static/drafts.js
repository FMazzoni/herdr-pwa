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

  /* While a question waits a prompt is refused, so the composer is held: greyed, Send off, and
   * the draft left in the box, never sent. The answer region says when (data-hp-holds), and the
   * composer follows it after every redraw of either. */
  function follow() {
    var form = document.getElementById("composer");
    if (!form) {
      return;
    }
    var held = !!document.querySelector("#answer[data-hp-holds]");
    var box = form.querySelector("textarea[data-hp-draft]");
    var send = form.querySelector("button[type=submit]");
    form.classList.toggle("composer--held", held);
    if (box) {
      box.disabled = held;
      box.placeholder = box.getAttribute(held ? "data-hp-shut" : "data-hp-open") || box.placeholder;
    }
    if (send) {
      send.disabled = held;
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
    follow();
  });
  hp.bus.on("ready:restore", function () {
    settle(document);
    follow();
  });
})(window.hp);
