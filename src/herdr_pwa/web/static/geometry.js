/* herdr-pwa: the composer dock's geometry. The dock's height, the status row's over it and the iOS
 * keyboard's lift are measured here and written to <html> as --dock-h, --tray-h and --kb, only
 * when they change; app.css places the dock with them. A pinned reader is kept at the end as any
 * of them changes (hp.chatend). */
(function (hp) {
  "use strict";

  /* ---- The composer dock ----
   * The composer is fixed to the bottom. The page reserves the dock's real height, and
   * while the iOS keyboard is open the dock rides on top of it: the layout viewport keeps
   * its height there and the visible band pans inside it as the reader scrolls, so the dock
   * is lifted by the gap between the band's bottom and the layout viewport's. */
  var root = document.documentElement;
  var docking = false;
  var dockedBand = -1;
  var dockedH = -1;
  var trayH = -1;
  var liftedKb = -1;
  // The composer and the status row each grow on their own and are each swapped whole by htmx.
  var watched = { composer: null, tray: null };
  // The dock grows with iPhone Text Size and with any banner in it; nothing else says so.
  var sizes = typeof window.ResizeObserver === "function" ? new window.ResizeObserver(redock) : null;
  /* WebKit runs these scripts before a stylesheet still loading has applied: a landing then is
   * against the unstyled page, the sheet's arrival moves it, and that move reads as the reader's
   * scroll. The page lands once every sheet has loaded or failed. */
  var unstyled = Array.prototype.filter.call(document.querySelectorAll('link[rel="stylesheet"]'), function (link) {
    return !link.sheet;
  });
  unstyled.forEach(function (link) {
    function arrived() {
      unstyled = unstyled.filter(function (other) {
        return other !== link;
      });
      dockedBand = -1;
      redock();
    }
    link.addEventListener("load", arrived);
    link.addEventListener("error", arrived);
  });

  function watch(id) {
    var node = document.getElementById(id);
    if (sizes && watched[id] !== node) {
      if (watched[id]) {
        sizes.unobserve(watched[id]);
      }
      if (node) {
        sizes.observe(node);
      }
      watched[id] = node;
    }
    return node;
  }

  // The status row's height, written only when it changes; whether it did.
  function measureTray() {
    var tray = watch("agent-tray");
    var height = tray ? tray.offsetHeight : 0;
    if (height === trayH) {
      return false;
    }
    root.style.setProperty("--tray-h", height + "px");
    trayH = height;
    return true;
  }

  function dock() {
    docking = false;
    var bar = watch("composer");
    if (!bar) {
      return;
    }
    var band = lift();
    var height = bar.offsetHeight;
    var trayMoved = measureTray();
    // A pinned reader stays at the end as the keyboard opens or the dock grows; a pan of the band
    // is the reader's own scroll (or nothing), so it never moves the page.
    // A frame can land before chatend.js has run, when no reader has been put anywhere yet.
    var moved = band !== dockedBand || height !== dockedH || trayMoved;
    var wasEnd = moved && !unstyled.length && !!hp.chatend && hp.chatend.pinned();
    dockedBand = band;
    if (height !== dockedH) {
      root.style.setProperty("--dock-h", height + "px");
      dockedH = height;
    }
    if (wasEnd) {
      hp.chatend.toEnd();
    }
  }

  // The keyboard's lift alone, cheap enough to run inside a visualViewport event: iOS swaps the
  // band's offset mid-fling and draws the next frame with whatever lift the handler left.
  // Returns the band's height while the keyboard is open, else 0.
  function lift() {
    var view = window.visualViewport;
    // Against the layout viewport the dock is fixed to: WebKit reports innerHeight as the band.
    var layout = root.clientHeight;
    var keyboard = !!view && typing() && Math.round(layout - view.height * view.scale) > 0;
    var covered = keyboard ? Math.max(0, Math.round(layout - view.height - view.offsetTop)) : 0;
    // On <html> every write restyles the whole chat, so an unchanged lift is not written.
    if (covered !== liftedKb) {
      root.style.setProperty("--kb", covered + "px");
      liftedKb = covered;
    }
    // Open for as long as the band is short, not only while it sits above the layout's bottom.
    root.classList.toggle("is-keyboard", keyboard);
    return keyboard ? Math.round(view.height) : 0;
  }

  function viewMoved() {
    if (document.getElementById("composer")) {
      lift();
    }
    redock();
  }

  // Only a field that takes text raises the keyboard; a short band without one is the browser's bars.
  var NO_KEYS = /^(button|checkbox|radio|submit|reset|file|range|color|hidden|image)$/;
  function typing() {
    var field = document.activeElement;
    if (!field) {
      return false;
    }
    return field.tagName === "TEXTAREA" || field.isContentEditable
      || (field.tagName === "INPUT" && !NO_KEYS.test(field.type));
  }

  function redock() {
    if (!docking) {
      docking = true;
      window.requestAnimationFrame(dock);
    }
  }

  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", viewMoved);
    window.visualViewport.addEventListener("scroll", viewMoved);
  }
  window.addEventListener("resize", redock);
  document.addEventListener("focusin", redock);
  document.addEventListener("focusout", redock);
  document.addEventListener("input", redock);
  document.addEventListener("htmx:afterSettle", redock);

  /* The page arrives with its view, the question and the composer already drawn, so it lands as
   * its first swap used to: the dock's room reserved first, which lands a pinned page at the end. */
  hp.bus.on("ready:scroll", function () {
    // A frame before chatend.js ran may have measured the dock already, with no reader to land.
    dockedBand = -1;
    dock();
  });
})(window.hp);
