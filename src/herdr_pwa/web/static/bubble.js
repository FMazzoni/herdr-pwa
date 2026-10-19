/* herdr-pwa: the status row. A crest crosses its words while the agent works, its timer ticks
 * between redraws, it says when the log has gone quiet, and its sheet stays open across redraws.
 * It rewrites only the words: the counts beside them are the server's and stay as drawn. */
(function (hp) {
  "use strict";

  /* ---- The status bubble: a crest crosses its words while the agent works; its timer ticks ----
   * The server draws the bubble's state, words and clocks on every redraw; between redraws this
   * carries the timer on, says "No activity" once the log has been still too long (a log that
   * does not move redraws nothing), and runs at most one frame loop, only while the bubble is
   * working, on screen, in a visible page and allowed to move. Under Reduce Motion, or a motion
   * style that is still, the words stand still and the timer still ticks. VoiceOver is told only
   * when the state changes: working, quiet, needs you, done. */
  var stillness = window.matchMedia("(prefers-reduced-motion: reduce)");
  // Stop the animation, remembered on this device: every bubble wears the still motion (WCAG 2.2.2).
  var STILL = "hp-still";
  var held = hp.kept(STILL);
  var bubble = {
    node: null, stretch: "", start: 0, drawnAt: 0, ran: null, quiet: null, after: NaN,
    motion: null, glyphs: [], frame: 0, tick: 0, seen: false, observer: null,
  };
  // The state last told to VoiceOver, so a redraw of the same state says nothing.
  var bubbleState = null;

  // A number the server wrote into an attribute, or null where it wrote none.
  function attrNumber(raw) {
    return raw === null || raw === "" ? null : Number(raw);
  }

  function motionOf(node) {
    var name = held ? "still" : node.getAttribute("data-working-motion") || "";
    return Object.prototype.hasOwnProperty.call(hp.logic.MOTIONS, name) ? hp.logic.MOTIONS[name] : null;
  }

  // The words as one span per letter, which the crest colours one by one; VoiceOver skips them.
  function layGlyphs(node, motion) {
    var words = node.querySelector(".bubble__text");
    if (!words) {
      return [];
    }
    var glyphs = Array.from(words.textContent).map(function (letter) {
      var glyph = document.createElement("span");
      glyph.className = "bubble__glyph";
      glyph.textContent = letter;
      return glyph;
    });
    words.replaceChildren.apply(words, glyphs);
    node.classList.toggle("is-lifting", motion.rise > 0);
    node.style.setProperty("--working-rise", motion.rise + "em");
    return glyphs;
  }

  function drawCrest(now) {
    var b = bubble;
    var count = b.glyphs.length;
    var phase = ((now - b.start) % b.motion.period) / b.motion.period;
    b.glyphs.forEach(function (glyph, i) {
      var middle = i + 0.5;
      var side = hp.logic.glow(middle, count, phase, b.motion.reach);
      glyph.style.setProperty("--glyph-wave", hp.logic.wave(middle, count, phase, b.motion.reach).toFixed(3));
      // Three stops as two blends: the first two, then that and the third.
      glyph.style.setProperty("--glyph-near", Math.min(1, side * 2).toFixed(3));
      glyph.style.setProperty("--glyph-far", Math.max(0, side * 2 - 1).toFixed(3));
    });
  }

  function runCrest() {
    var b = bubble;
    var go = b.glyphs.length > 0 && b.seen && !document.hidden;
    if (go && !b.frame) {
      b.frame = window.requestAnimationFrame(function (now) {
        b.frame = 0;
        if (!b.node || !b.node.isConnected) {
          stopBubble();
          return;
        }
        drawCrest(now);
        runCrest();
      });
    } else if (!go && b.frame) {
      window.cancelAnimationFrame(b.frame);
      b.frame = 0;
    }
  }

  function stopCrest() {
    var b = bubble;
    if (b.frame) {
      window.cancelAnimationFrame(b.frame);
      b.frame = 0;
    }
    if (b.observer) {
      b.observer.disconnect();
      b.observer = null;
    }
    var words = b.node ? b.node.querySelector(".bubble__text") : null;
    if (words && b.glyphs.length) {
      // The letters' own text, back as one run of words.
      var text = words.textContent;
      words.textContent = text;
    }
    b.glyphs = [];
  }

  function stopBubble() {
    stopCrest();
    if (bubble.tick) {
      window.clearTimeout(bubble.tick);
      bubble.tick = 0;
    }
    bubble.node = null;
  }

  // The bubble's own words, and VoiceOver's, once this page has seen the log stay still too long.
  function showQuiet(node, seconds) {
    var words = node.querySelector(".bubble__text");
    var row = node.closest(".tray__row");
    var described = row ? row.querySelector(".tray__said") : null;
    if (words) {
      words.textContent = hp.logic.quietFor(seconds);
    }
    if (described) {
      described.textContent = "No recent activity";
    }
  }

  // The row drawn quiet as the server would draw it: a grey dot, one duration, the page's word.
  function quietRow(node) {
    var row = node.closest(".tray__row");
    if (row) {
      row.classList.replace("tray__row--working", "tray__row--quiet");
      var dot = row.querySelector(".dot");
      var time = row.querySelector(".tray__time");
      if (dot) {
        dot.className = "dot dot--updating";
      }
      if (time) {
        time.remove();
      }
    }
    var word = document.getElementById("agent-status");
    if (word && word.textContent === "working") {
      word.textContent = "quiet";
    }
  }

  function goQuiet(node, seconds) {
    stopCrest();
    quietRow(node);
    node.classList.remove("bubble--working");
    node.classList.add("bubble--quiet");
    node.setAttribute("data-hp-bubble", "quiet");
    node.setAttribute("data-hp-said", "No recent activity");
    showQuiet(node, seconds);
    tellBubble();
  }

  // Once a second, on the second: the timer, and whether the turn has gone quiet.
  function tickBubble() {
    var b = bubble;
    b.tick = 0;
    if (!b.node || !b.node.isConnected) {
      stopBubble();
      return;
    }
    var since = window.performance.now() - b.drawnAt;
    var row = b.node.closest(".tray__row");
    var time = row ? row.querySelector(".bubble__time") : null;
    if (time && b.ran !== null) {
      time.textContent = hp.logic.elapsed(b.ran + since / 1000);
    }
    var state = b.node.getAttribute("data-hp-bubble");
    if (b.quiet !== null && state === "working" && hp.logic.goneQuiet(b.quiet, since, b.after)) {
      goQuiet(b.node, b.quiet + since / 1000);
    } else if (b.quiet !== null && state === "quiet") {
      showQuiet(b.node, b.quiet + since / 1000);
    }
    if (!document.hidden) {
      b.tick = window.setTimeout(tickBubble, 1000 - (since % 1000));
    }
  }

  /* Says the bubble's state when it is not the one last said: the first drawing is the page
   * opening, not news. A finished turn's receipt settles in where the bubble was. */
  // The Pane tab shows the row but says nothing of it: there the terminal is what is being read.
  var voiced = !!document.querySelector('main[data-hp-tab="chat"]');

  function tellBubble() {
    var shown = document.querySelector("#agent-tray [data-hp-bubble]");
    var state = shown ? shown.getAttribute("data-hp-bubble") : "";
    if (voiced && bubbleState !== null && state !== bubbleState) {
      if (shown) {
        hp.voice.say(shown.getAttribute("data-hp-said") || "");
      }
      if (state === "done" && bubbleState !== "" && !held) {
        shown.classList.add("is-folding");
      }
    }
    bubbleState = state;
  }

  function startBubble() {
    var node = document.querySelector("#agent-tray .bubble[data-hp-bubble]");
    if (node === bubble.node) {
      return;
    }
    stopBubble();
    if (!node) {
      return;
    }
    var b = bubble;
    var stretch = node.getAttribute("data-hp-stretch") || "";
    b.drawnAt = window.performance.now();
    // A redraw of the same stretch carries the crest on from where it was.
    if (stretch !== b.stretch) {
      b.stretch = stretch;
      b.start = b.drawnAt;
    }
    b.node = node;
    b.ran = attrNumber(node.getAttribute("data-hp-ran"));
    b.quiet = attrNumber(node.getAttribute("data-hp-quiet"));
    b.after = attrNumber(node.getAttribute("data-hp-quiet-after"));
    startCrest(node);
    tickBubble();
  }

  // The crest, where the bubble is working and its motion may move.
  function startCrest(node) {
    var b = bubble;
    b.motion = motionOf(node);
    if (node.getAttribute("data-hp-bubble") !== "working" || !b.motion || stillness.matches) {
      return;
    }
    b.glyphs = layGlyphs(node, b.motion);
    drawCrest(window.performance.now());
    b.observer = new IntersectionObserver(function (entries) {
      b.seen = entries[entries.length - 1].isIntersecting;
      runCrest();
    });
    b.observer.observe(node);
  }

  // A new motion for the bubble on screen; its timer and quiet clock carry on as they were.
  function restyleBubble() {
    stopCrest();
    if (bubble.node) {
      startCrest(bubble.node);
    }
  }

  function wakeBubble() {
    runCrest();
    if (bubble.node && !bubble.tick && !document.hidden) {
      tickBubble();
    }
  }

  // The switch is on the alerts page, a preference of this device: it shows what this device chose.
  function showStill() {
    var button = document.querySelector("[data-hp-still]");
    if (button) {
      button.setAttribute("aria-pressed", held ? "true" : "false");
      (button.closest("[hidden]") || button.parentNode).hidden = false;
    }
  }

  function showBubble() {
    showStill();
    tellBubble();
    startBubble();
  }
  hp.bus.on("ready:announce", showBubble);
  hp.bus.on("swapped:announce", showBubble);
  document.addEventListener("visibilitychange", wakeBubble);
  window.addEventListener("pageshow", wakeBubble);
  stillness.addEventListener("change", restyleBubble);

  // The switch is on the alerts page: an agent page open in another tab follows the change.
  window.addEventListener("storage", function (event) {
    if (event.key === STILL) {
      held = event.newValue === "1";
      showStill();
      restyleBubble();
    }
  });

  document.addEventListener("click", function (event) {
    var button = event.target && event.target.closest ? event.target.closest("[data-hp-still]") : null;
    if (!button) {
      return;
    }
    held = button.getAttribute("aria-pressed") !== "true";
    hp.keep(STILL, held);
    showStill();
    restyleBubble();
  });

  /* ---- The row's sheet stays open across the redraws that refresh the row ---- */
  var sheetOpen = false;

  hp.bus.on("swap:capture", function (detail) {
    var target = detail.target;
    if (target && target.id === "agent-tray") {
      var sheet = target.querySelector("details");
      sheetOpen = !!(sheet && sheet.open);
    }
  });

  hp.bus.on("swapped:restore", function () {
    var sheet = sheetOpen ? document.querySelector("#agent-tray details") : null;
    sheetOpen = false;
    if (sheet) {
      sheet.open = true;
    }
  });
})(window.hp);
