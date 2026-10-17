/* herdr-pwa: an armed interrupt disarms itself, never under a thumb. */
(function (hp) {
  "use strict";

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
      if (Date.now() - lastTouch < 1500 || hp.sendhold.sending()) {
        disarmLater(node, 1.5);
        return;
      }
      hp.refetch(region);
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
  document.addEventListener("DOMContentLoaded", function () {
    armTimers(document);
  });
})(window.hp);
