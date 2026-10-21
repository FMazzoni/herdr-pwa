/* herdr-pwa device report: debug only, linked into pages solely when HP_DEVICE_REPORT=1.
 * Measures what no desktop engine reproduces (iOS keyboard, URL bar, safe areas,
 * standalone mode, the edge-swipe back) and posts it in batches to /device-report.
 * It reads the page and never changes it, apart from one hidden probe element. */
(function () {
  "use strict";

  var ENDPOINT = "/device-report";
  var SESSION_KEY = "hp-devreport-session";
  var START_KEY = "hp-devreport-start";
  var PAGE_KEY = "hp-devreport-page";
  var FLUSH_AT = 25;
  var FLUSH_MS = 5000;
  var MAX_PER_PAGE = 400;
  var MAX_LISTED = 25;
  var MIN_TAP = 44;

  function stored(key) {
    try {
      return window.sessionStorage.getItem(key);
    } catch (e) {
      return null;
    }
  }

  function store(key, value) {
    try {
      window.sessionStorage.setItem(key, value);
    } catch (e) {
      /* A private window loses the chain, not the report. */
    }
  }

  function newId() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return window.crypto.randomUUID();
    }
    var bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    var out = "";
    for (var i = 0; i < bytes.length; i++) {
      out += (bytes[i] + 256).toString(16).slice(1);
    }
    return out;
  }

  /* sessionStorage survives navigations within one tab, so a load chain shares an id. */
  var session = stored(SESSION_KEY);
  var chainStart = Number(stored(START_KEY));
  if (!session || !chainStart) {
    session = newId();
    chainStart = Date.now();
    store(SESSION_KEY, session);
    store(START_KEY, String(chainStart));
  }
  var page = Number(stored(PAGE_KEY) || "0") + 1;
  store(PAGE_KEY, String(page));

  var queue = [];
  var seq = 0;
  var recorded = 0;
  var dropped = 0;
  var timer = null;

  function flush(leaving) {
    if (timer !== null) {
      window.clearTimeout(timer);
      timer = null;
    }
    if (!queue.length) {
      return;
    }
    seq += 1;
    var body = JSON.stringify({ session: session, page: page, seq: seq, events: queue });
    queue = [];
    /* keepalive lets the post outlive a pagehide, so a swipe-back is still captured. */
    fetch(ENDPOINT, {
      method: "POST",
      credentials: "same-origin",
      keepalive: !!leaving,
      headers: { "Content-Type": "application/json" },
      body: body,
    }).catch(function () {
      /* A lost report is a gap in the log, never something to show the person using the page. */
    });
  }

  function record(kind, detail) {
    if (recorded >= MAX_PER_PAGE) {
      dropped += 1;
      return;
    }
    recorded += 1;
    var event = { t: Date.now() - chainStart, page: page, kind: kind };
    for (var key in detail) {
      if (Object.prototype.hasOwnProperty.call(detail, key)) {
        event[key] = detail[key];
      }
    }
    queue.push(event);
    if (queue.length >= FLUSH_AT) {
      flush(false);
    } else if (timer === null) {
      timer = window.setTimeout(function () {
        flush(false);
      }, FLUSH_MS);
    }
  }

  function round(n) {
    return Math.round(n * 10) / 10;
  }

  function standalone() {
    var media = window.matchMedia && window.matchMedia("(display-mode: standalone)").matches;
    return !!(media || window.navigator.standalone);
  }

  /* env() only resolves inside CSS, so a hidden probe carries it as padding and is read back. */
  function safeAreas() {
    var probe = document.createElement("div");
    probe.setAttribute("aria-hidden", "true");
    var s = probe.style;
    s.setProperty("position", "fixed");
    s.setProperty("top", "0");
    s.setProperty("left", "0");
    s.setProperty("visibility", "hidden");
    s.setProperty("pointer-events", "none");
    s.setProperty("padding-top", "env(safe-area-inset-top)");
    s.setProperty("padding-right", "env(safe-area-inset-right)");
    s.setProperty("padding-bottom", "env(safe-area-inset-bottom)");
    s.setProperty("padding-left", "env(safe-area-inset-left)");
    document.body.appendChild(probe);
    var cs = window.getComputedStyle(probe);
    var out = {
      top: cs.paddingTop,
      right: cs.paddingRight,
      bottom: cs.paddingBottom,
      left: cs.paddingLeft,
    };
    probe.remove();
    return out;
  }

  // What the dock drew: where the composer and top bar sit, and the lift and keyboard class it set.
  function docked() {
    var root = document.documentElement;
    var out = {
      kb: root.style.getPropertyValue("--kb"),
      keyboard: root.classList.contains("is-keyboard"),
    };
    var composer = document.getElementById("composer");
    if (composer) {
      var c = composer.getBoundingClientRect();
      out.composer = { top: round(c.top), bottom: round(c.bottom) };
    }
    var top = document.querySelector(".topbar");
    if (top) {
      var t = top.getBoundingClientRect();
      out.topbar = { top: round(t.top), bottom: round(t.bottom) };
    }
    return out;
  }

  function viewport() {
    var vv = window.visualViewport;
    var out = {
      innerWidth: window.innerWidth,
      innerHeight: window.innerHeight,
      clientWidth: document.documentElement.clientWidth,
      scrollY: round(window.scrollY),
      // The layout viewport, which the dock measures the keyboard against; innerHeight is not it on iOS.
      clientHeight: document.documentElement.clientHeight,
      standalone: window.matchMedia("(display-mode: standalone)").matches || window.navigator.standalone === true,
      dock: docked(),
    };
    if (vv) {
      out.vv = {
        width: round(vv.width),
        height: round(vv.height),
        offsetTop: round(vv.offsetTop),
        pageTop: round(vv.pageTop),
        scale: round(vv.scale),
      };
    }
    return out;
  }

  function describe(el) {
    var name = el.tagName.toLowerCase();
    if (el.id) {
      name += "#" + el.id;
    }
    var cls = typeof el.className === "string" ? el.className.trim() : "";
    if (cls) {
      name += "." + cls.split(/\s+/).slice(0, 3).join(".");
    }
    return name;
  }

  function navigationType() {
    var entries = window.performance && window.performance.getEntriesByType
      ? window.performance.getEntriesByType("navigation")
      : [];
    return entries.length ? entries[0].type : "unknown";
  }

  function where() {
    return { url: window.location.pathname + window.location.search, historyLength: window.history.length };
  }

  function editing() {
    var el = document.activeElement;
    if (!el) {
      return null;
    }
    var tag = el.tagName.toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select" || el.isContentEditable) {
      return el;
    }
    return null;
  }

  function overflowing() {
    var limit = document.documentElement.clientWidth + 0.5;
    var found = [];
    var all = document.body.getElementsByTagName("*");
    for (var i = 0; i < all.length && found.length < MAX_LISTED; i++) {
      var rect = all[i].getBoundingClientRect();
      if (rect.width > 0 && (rect.right > limit || rect.left < -0.5)) {
        found.push({ el: describe(all[i]), left: round(rect.left), right: round(rect.right), width: round(rect.width) });
      }
    }
    return { scrollWidth: document.documentElement.scrollWidth, limit: round(limit), elements: found };
  }

  function smallTargets() {
    var nodes = document.querySelectorAll("a[href], button, input, select, textarea, summary, [role=button], [tabindex]");
    var found = [];
    for (var i = 0; i < nodes.length && found.length < MAX_LISTED; i++) {
      var rect = nodes[i].getBoundingClientRect();
      var shown = rect.width > 0 && rect.height > 0;
      if (shown && (rect.width < MIN_TAP || rect.height < MIN_TAP)) {
        found.push({ el: describe(nodes[i]), width: round(rect.width), height: round(rect.height) });
      }
    }
    return { count: found.length, elements: found };
  }

  /* Polls swap every few seconds, so a layout is recorded only when it differs. */
  var lastLayout = "";
  function survey(reason) {
    var layout = { overflow: overflowing(), smallTargets: smallTargets() };
    var key = JSON.stringify(layout);
    if (key === lastLayout) {
      return;
    }
    lastLayout = key;
    layout.reason = reason;
    record("layout", layout);
  }

  /* ---- what the page was when it loaded ---- */

  var load = where();
  load.navigationType = navigationType();
  load.standalone = standalone();
  load.safeArea = safeAreas();
  load.viewport = viewport();
  load.screen = { width: window.screen.width, height: window.screen.height, dpr: window.devicePixelRatio };
  load.referrer = document.referrer;
  load.userAgent = window.navigator.userAgent;
  record("load", load);
  window.setTimeout(function () {
    survey("load");
  }, 300);

  /* ---- viewport over time, and the keyboard while an input has focus ---- */

  var lastViewport = "";
  var pending = null;
  function viewportChanged(source) {
    if (pending !== null) {
      return;
    }
    pending = window.setTimeout(function () {
      pending = null;
      var now = viewport();
      var key = JSON.stringify(now);
      if (key === lastViewport) {
        return;
      }
      lastViewport = key;
      var detail = { source: source, viewport: now };
      var field = editing();
      if (field && window.visualViewport) {
        detail.keyboard = round(window.innerHeight - window.visualViewport.height);
        detail.focused = describe(field);
      }
      record(field ? "keyboard" : "viewport", detail);
    }, 150);
  }
  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", function () {
      viewportChanged("vv-resize");
    });
    window.visualViewport.addEventListener("scroll", function () {
      viewportChanged("vv-scroll");
    });
  }
  window.addEventListener("resize", function () {
    viewportChanged("resize");
  });
  window.addEventListener("orientationchange", function () {
    viewportChanged("orientation");
  });
  document.addEventListener("focusin", function () {
    var field = editing();
    if (field) {
      record("focus", { el: describe(field), viewport: viewport() });
    }
  });
  document.addEventListener("focusout", function (event) {
    if (event.target && event.target.tagName) {
      record("blur", { el: describe(event.target) });
    }
  });

  /* ---- navigation: the back gesture shows up as pageshow persisted or popstate ---- */

  window.addEventListener("pageshow", function (event) {
    var detail = where();
    detail.persisted = event.persisted;
    detail.navigationType = navigationType();
    detail.viewport = viewport();
    record("pageshow", detail);
    if (event.persisted) {
      flush(false);
    }
  });
  window.addEventListener("pagehide", function (event) {
    var detail = where();
    detail.persisted = event.persisted;
    record("pagehide", detail);
    flush(true);
  });
  window.addEventListener("popstate", function () {
    record("popstate", where());
  });
  window.addEventListener("hashchange", function () {
    record("hashchange", where());
  });
  document.addEventListener("visibilitychange", function () {
    record("visibility", { state: document.visibilityState });
    if (document.hidden) {
      flush(true);
    }
  });
  document.addEventListener("htmx:afterSettle", function () {
    if (pending === null) {
      survey("swap");
    }
  });
  window.addEventListener("pagehide", function () {
    if (dropped) {
      queue.push({ t: Date.now() - chainStart, page: page, kind: "dropped", count: dropped });
      flush(true);
    }
  });
})();
