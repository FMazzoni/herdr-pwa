/* herdr-pwa: an armed interrupt disarms itself, never under a thumb; the status row's sheet arms
 * Stop in place, and no control in it changes role under a finger. */
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

  /* ---- Stop, armed in place in the row's sheet ----
   * A first tap on Stop… (or on Esc… while the agent works) only arms: each such slot then holds
   * Keep going, so the second tap of a double tap lands on the safe answer, and the filled Stop now
   * waits on the far side. It disarms after 5s, never under a thumb. Once a stop is sent, Stop… is
   * held as "Stopping…" for 3s, since a second Ctrl+C at an idle prompt quits the harness. The
   * server still holds an unconfirmed key, for a page with no script or a stale one. */
  var DISARM_MS = 5000;
  var STOPPING_MS = 3000;
  // A control that just changed role takes no tap for this long (docs/design.md, G3).
  var SETTLE_MS = 300;
  var arming = "";
  var armedAt = 0;
  var stoppedAt = 0;
  var changedAt = 0;
  var touched = null;
  var trayShape = "";

  function tray() {
    return document.getElementById("agent-tray");
  }

  function keyOf(button) {
    var form = button.closest("form");
    var input = form ? form.querySelector('input[name="key"]') : null;
    return input ? input.value : "";
  }

  // Every control the tray draws, in order, as the role each plays now: what a finger can land on.
  function shapeOf(node) {
    var parts = [];
    var controls = node ? node.querySelectorAll("button, summary") : [];
    for (var i = 0; i < controls.length; i++) {
      var shown = controls[i].offsetParent !== null || controls[i].tagName === "SUMMARY";
      parts.push(shown ? controls[i].className + ":" + controls[i].textContent.trim() : "-");
    }
    return parts.join("|");
  }

  function toggle(nodes, hide) {
    for (var i = 0; i < nodes.length; i++) {
      nodes[i].hidden = hide(nodes[i]);
    }
  }

  function drawArm() {
    var node = tray();
    var head = node ? node.querySelector(".tray__head") : null;
    if (!head) {
      return;
    }
    var armed = head.querySelector(".tray__armed");
    if (!armed) {
      arming = "";
    }
    head.setAttribute("data-hp-arming", arming);
    toggle(head.querySelectorAll(".tray__title"), function () { return !!arming; });
    toggle(head.querySelectorAll(".tray__ask"), function () { return !arming; });
    if (armed) {
      armed.hidden = !arming;
      toggle(armed.querySelectorAll("[data-hp-for]"), function (go) {
        return go.getAttribute("data-hp-for") !== arming;
      });
    }
    toggle(node.querySelectorAll(".tray__arm"), function () { return !!arming; });
    toggle(node.querySelectorAll(".tray__slot [data-hp-keep]"), function () { return !arming; });
    var stopping = Date.now() - stoppedAt < STOPPING_MS;
    var stops = node.querySelectorAll(".tray__slot--stop .tray__arm");
    for (var i = 0; i < stops.length; i++) {
      stops[i].disabled = stopping;
    }
    toggle(head.querySelectorAll(".tray__stopping"), function () { return !stopping; });
    trayShape = shapeOf(node);
  }

  function setArming(key) {
    arming = key;
    armedAt = Date.now();
    changedAt = armedAt;
    drawArm();
    if (key) {
      disarmArmed(armedAt, DISARM_MS);
    }
  }

  // Disarms the arm made at `at`, unless a newer one replaced it or a thumb is on the screen.
  function disarmArmed(at, ms) {
    window.setTimeout(function () {
      if (!arming || armedAt !== at) {
        return;
      }
      if (Date.now() - lastTouch < 1500) {
        disarmArmed(at, 1500);
        return;
      }
      setArming("");
    }, ms);
  }

  function closeSheet() {
    var sheet = document.querySelector("#agent-tray details[open]");
    if (sheet) {
      sheet.open = false;
      setArming("");
    }
  }

  // The control under the finger as the touch began, and the role it played then.
  document.addEventListener("pointerdown", function (event) {
    var control = event.target && event.target.closest ? event.target.closest("#agent-tray button, #agent-tray summary") : null;
    touched = control ? { control: control, shape: trayShape } : null;
  }, true);

  // A tap acts on the state drawn when its touch began: one on a control that changed role since,
  // or that changed role a moment ago, does nothing.
  function settled(control) {
    if (Date.now() - changedAt < SETTLE_MS) {
      return false;
    }
    return !touched || (touched.control === control && touched.shape === trayShape);
  }

  // A tap from a key, not a finger, has no touch to have begun on anything else.
  function fromKey(event) {
    return event.detail === 0;
  }

  function onTrayTap(event, control) {
    if (!fromKey(event) && !settled(control)) {
      event.preventDefault();
      event.stopImmediatePropagation();
      return;
    }
    if (control.matches(".tray__arm")) {
      event.preventDefault();
      setArming(keyOf(control));
    } else if (control.matches("[data-hp-keep]")) {
      event.preventDefault();
      setArming("");
    } else if (control.matches("[data-hp-close]")) {
      event.preventDefault();
      closeSheet();
    } else if (control.matches(".button--stop")) {
      stoppedAt = Date.now();
      arming = "";
      // Drawn after htmx has read the form, so the confirmed key still goes.
      window.setTimeout(drawArm, 0);
      window.setTimeout(drawArm, STOPPING_MS + 50);
    }
  }

  // A tap outside an open sheet closes it; on the chat behind it, it does nothing else.
  function onOutsideTap(event) {
    var open = document.querySelector("#agent-tray details[open]");
    var target = event.target;
    if (!open || !target || !target.closest || target.closest("#agent-tray")) {
      return;
    }
    closeSheet();
    if (target.closest("main") && !target.closest("#composer")) {
      event.preventDefault();
      event.stopImmediatePropagation();
    }
  }

  document.addEventListener("click", function (event) {
    var target = event.target;
    var control = target && target.closest ? target.closest("#agent-tray button, #agent-tray summary") : null;
    if (control) {
      onTrayTap(event, control);
    } else {
      onOutsideTap(event);
    }
    touched = null;
  }, true);

  // htmx enables the tray's buttons again when a write lands, Stopping… or not.
  document.addEventListener("htmx:afterSettle", function () {
    if (Date.now() - stoppedAt < STOPPING_MS) {
      drawArm();
    }
  });

  // A redraw keeps what is armed or stopping; one that changed what a control does is a change.
  hp.bus.on("swapped:restore", function (detail) {
    if (!detail || !detail.target || detail.target.id !== "agent-tray") {
      return;
    }
    var before = trayShape;
    drawArm();
    if (before !== trayShape) {
      changedAt = Date.now();
    }
  });
  hp.bus.on("ready:restore", drawArm);
  document.addEventListener("toggle", function (event) {
    if (event.target && event.target.matches && event.target.matches("#agent-tray details") && !event.target.open) {
      setArming("");
    }
  }, true);
})(window.hp);
