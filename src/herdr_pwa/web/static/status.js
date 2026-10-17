/* herdr-pwa: the agent's status around the page. The stats sheet stays open across its redraws,
 * the status word and dot follow the answer region, and a finish shown on screen is marked read. */
(function (hp) {
  "use strict";

  /* ---- The stats strip keeps its details open across the redraws that refresh it ---- */
  var statsOpen = false;

  hp.bus.on("swap:capture", function (detail) {
    var target = detail.target;
    if (target && target.id === "agent-stats") {
      var sheet = target.querySelector("details");
      statsOpen = !!(sheet && sheet.open);
    }
  });

  // Before the page is given its place: the sheet is in the sticky bar, so its height is the page's.
  hp.bus.on("swapped:restore", function () {
    var sheet = statsOpen ? document.querySelector("#agent-stats details") : null;
    statsOpen = false;
    if (sheet) {
      sheet.open = true;
    }
  });

  /* ---- Seen: a finish the phone has shown is not news ----
   * Said on load and again each time the page comes back into view. */

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
  document.addEventListener("visibilitychange", markSeen);

  /* The answer region is what the stream redraws for this agent, and it carries the status the
   * page shows; the status word and dot follow it, and a finish seen on screen is marked read. */
  var statusWord = document.getElementById("agent-status");
  var shownStatus = statusWord ? statusWord.textContent : "";
  document.addEventListener("htmx:afterSettle", function () {
    var region = document.getElementById("answer");
    var status = region ? region.getAttribute("data-hp-status") : null;
    if (!status || status === shownStatus) {
      return;
    }
    var word = document.getElementById("agent-status");
    var dot = document.getElementById("agent-dot");
    if (word) {
      word.textContent = status;
    }
    if (dot) {
      dot.className = "dot dot--" + status;
    }
    if (hp.logic.finishedNow(shownStatus, status)) {
      markSeen();
    }
    shownStatus = status;
  });
})(window.hp);
