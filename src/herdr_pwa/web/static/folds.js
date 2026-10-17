/* herdr-pwa: a fold the reader opened stays open across the redraws of the view. */
(function (hp) {
  "use strict";

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
    return (unit ? hp.chatend.unitKey(unit) : "") + "#" + (own || Array.prototype.indexOf.call(inside, details));
  }

  hp.bus.on("swap:capture", function (detail) {
    if (!isView(detail)) {
      return;
    }
    unfolded = [];
    drawn = [];
    var all = detail.target.querySelectorAll("details");
    for (var i = 0; i < all.length; i++) {
      drawn.push(detailsKey(all[i]));
      if (all[i].open) {
        unfolded.push(detailsKey(all[i]));
      }
    }
  });

  hp.bus.on("swapped:restore", function (detail) {
    if (!isView(detail) || !unfolded.length) {
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
})(window.hp);
