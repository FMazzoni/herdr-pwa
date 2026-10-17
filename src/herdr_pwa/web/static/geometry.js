/* herdr-pwa: the composer dock's geometry. The dock's height and the iOS keyboard's lift are
 * measured here and written to <html> as --dock-h and --kb, only when they change; app.css places
 * the dock with them. A pinned reader is kept at the end as either changes (hp.chatend). */
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
  var liftedKb = -1;
  var watched = null;
  // The dock grows with iPhone Text Size and with any banner in it; nothing else says so.
  var sizes = typeof window.ResizeObserver === "function" ? new window.ResizeObserver(redock) : null;

  function dock() {
    docking = false;
    var bar = document.getElementById("composer");
    if (!bar) {
      return;
    }
    if (sizes && watched !== bar) {
      if (watched) {
        sizes.unobserve(watched);
      }
      sizes.observe(bar);
      watched = bar;
    }
    var band = lift();
    var height = bar.offsetHeight;
    // A pinned reader stays at the end as the keyboard opens or the dock grows; a pan of the band
    // is the reader's own scroll (or nothing), so it never moves the page.
    var wasEnd = (band !== dockedBand || height !== dockedH) && hp.chatend.pinned();
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
  hp.bus.on("ready:scroll", dock);
})(window.hp);
