// pcb-argue.js — the board page's one text box (docs/backlog/pcb-argue-with-design.md).
//
// The board render is an <object type="image/svg+xml"> on purpose (its
// layer-toggle legend script must run as its own document), so a
// [data-handle] listener on THIS page never fires for it. Both documents
// are same-origin, so we reach into objEl.contentDocument and listen
// there. The part-body hit rects sit UNDER the layer stack (gerber_view
// _part_body_els), so a click is resolved by walking elementsFromPoint
// top-down to the first element carrying a handle — pads and tracks win
// over the body they belong to; a click on bare mask inside a part's
// pad extent resolves to the part.
//
// Degrades: contentDocument === null (blocked / not loaded) leaves a text
// box that still submits whole-design arguments. A click that does
// nothing is acceptable; a page that breaks is not.
(function () {
  "use strict";
  var root = document.getElementById("pcb-argue");
  var box = document.getElementById("pcb-argue-text");
  if (!root || !box) return;
  var noteUrl = root.dataset.noteUrl;
  var status = document.getElementById("pcb-argue-status");
  var errorEl = document.getElementById("pcb-argue-error");
  var submit = document.getElementById("pcb-argue-submit");
  var host = document.getElementById("pcb-notes-host");
  var chipsEl = document.getElementById("pcb-argue-chips");
  var clearBtn = document.getElementById("pcb-argue-clear");
  var modeAsk = document.getElementById("pcb-mode-ask");
  var modePropose = document.getElementById("pcb-mode-propose");
  var chatForm = document.getElementById("design-chat-form");
  var chatMsg = document.getElementById("design-chat-message");
  var chatHandles = document.getElementById("design-chat-handles");
  var mode = "ask";
  var clicked = [];
  var shown = {}; // raw handle -> the text inserted into the box

  // Layout-only objects (vias, tracks, pours, zones — catalogue keys with
  // a via:/silk:/outline:/fiducial: prefix) carry the L: prefix; a layout
  // pin is the same object as the netlist pin and gets none. Mirrored
  // server-side by precis.pcb.argue.display_handle (the notes ledger).
  function displayOf(handle) {
    var kind = handle.split(":")[0];
    if (["via", "silk", "outline", "fiducial"].indexOf(kind) !== -1) {
      return "L:" + handle.replace(/:/g, " ");
    }
    return handle;
  }

  function renderChips() {
    if (!chipsEl) return;
    chipsEl.textContent = "";
    clicked.forEach(function (h) {
      var chip = document.createElement("button");
      chip.type = "button";
      chip.className = "rounded-full bg-surface-muted px-1.5 py-0.5";
      chip.textContent = displayOf(h) + " ×";
      chip.title = "remove " + displayOf(h);
      chip.addEventListener("click", function () {
        clicked = clicked.filter(function (x) { return x !== h; });
        renderChips();
      });
      chipsEl.appendChild(chip);
    });
  }

  function setMode(m) {
    mode = m;
    if (modeAsk) modeAsk.setAttribute("aria-pressed", String(m === "ask"));
    if (modePropose) modePropose.setAttribute("aria-pressed", String(m === "propose"));
    if (modeAsk) modeAsk.classList.toggle("font-semibold", m === "ask");
    if (modePropose) modePropose.classList.toggle("font-semibold", m === "propose");
  }
  if (modeAsk) modeAsk.addEventListener("click", function () { setMode("ask"); });
  if (modePropose) modePropose.addEventListener("click", function () { setMode("propose"); });
  if (clearBtn) clearBtn.addEventListener("click", function () {
    box.value = "";
    clicked = [];
    shown = {};
    renderChips();
    if (errorEl) errorEl.classList.add("hidden");
    if (status) status.textContent = "";
  });

  function insertHandle(handle) {
    var v = box.value;
    var start = box.selectionStart == null ? v.length : box.selectionStart;
    var end = box.selectionEnd == null ? start : box.selectionEnd;
    var before = v.slice(0, start);
    var after = v.slice(end);
    var lead = before && !/\s$/.test(before) ? " " : "";
    var trail = after && !/^\s/.test(after) ? " " : " ";
    var text = displayOf(handle);
    shown[handle] = text;
    var ins = lead + text + trail;
    box.value = before + ins + after;
    var caret = start + ins.length;
    box.setSelectionRange(caret, caret);
    box.focus({ preventScroll: true });
    if (clicked.indexOf(handle) === -1) clicked.push(handle);
    renderChips();
  }

  function handleAt(doc, ev) {
    var els = doc.elementsFromPoint
      ? doc.elementsFromPoint(ev.clientX, ev.clientY)
      : [ev.target];
    // A point on the pinned legend is the legend's (gr543695): never argue
    // the board object painted beneath it.
    if (window.PrecisViewport && window.PrecisViewport.overHud(els)) return null;
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      var hit = el && el.closest ? el.closest("[data-handle]") : null;
      if (hit) return hit.getAttribute("data-handle");
    }
    return null;
  }

  function wire(doc) {
    if (!doc || doc.__pcbArgueWired) return;
    doc.__pcbArgueWired = true;
    doc.addEventListener("click", function (ev) {
      // pcb-selection.js owns the click when it bound this document; it
      // announces the handle via pcb-selection-attach (never insert twice).
      if (doc.__pcbSelectionWired) return;
      var h = handleAt(doc, ev);
      if (h) insertHandle(h);
    });
  }

  document.addEventListener("pcb-selection-attach", function (ev) {
    if (ev.detail && ev.detail.handle) insertHandle(ev.detail.handle);
  });

  var obj = document.getElementById("pcb-board-object");
  if (obj) {
    obj.addEventListener("load", function () {
      try {
        wire(obj.contentDocument);
      } catch (e) {
        /* cross-origin or blocked: whole-design arguments still work */
      }
    });
    try {
      if (obj.contentDocument && obj.contentDocument.readyState === "complete") {
        wire(obj.contentDocument);
      }
    } catch (e) {
      /* same degradation */
    }
  }

  function showError(msg) {
    errorEl.textContent = msg;
    errorEl.classList.remove("hidden");
  }

  function send() {
    var text = box.value;
    if (!text.trim()) {
      showError("type something first");
      return;
    }
    // Only handles still present in the text (as the L:-display form or the
    // raw key) count as clicked.
    var present = clicked.filter(function (h) {
      return text.indexOf(shown[h] || h) !== -1 || text.indexOf(h) !== -1;
    });
    if (mode === "propose") {
      // The shared design-chat form carries the proposal: typed ops only,
      // never applied without the explicit Apply in the transcript below.
      if (!chatForm || !chatMsg) { showError("design chat unavailable"); return; }
      chatMsg.value = text;
      if (chatHandles) chatHandles.value = present.join(",");
      errorEl.classList.add("hidden");
      status.textContent = "proposing…";
      chatForm.requestSubmit();
      return;
    }
    errorEl.classList.add("hidden");
    status.textContent = "asking the model…";
    submit.disabled = true;
    fetch(noteUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text, handles: present }),
    })
      .then(function (r) {
        return r.json().then(function (data) {
          return { ok: r.ok, data: data };
        });
      })
      .then(function (res) {
        submit.disabled = false;
        var d = res.data || {};
        if (!res.ok) {
          var msg = d.error || "could not save";
          if (d.valid) {
            var roster = []
              .concat(d.valid.parts || [], d.valid.nets || [], d.valid.features || [])
              .slice(0, 40);
            if (roster.length) msg += " — valid: " + roster.join(" ");
          }
          showError(msg);
          status.textContent = "";
          return;
        }
        if (d.html && host) host.innerHTML = d.html;
        status.textContent = d.degraded
          ? "recorded as " + d.name + " — the model did not answer (" + (d.detail || "unavailable") + ")"
          : "recorded as " + d.name + ", answered as " + d.answer;
        box.value = "";
        clicked = [];
        shown = {};
        renderChips();
      })
      .catch(function (e) {
        submit.disabled = false;
        status.textContent = "";
        showError("request failed: " + e);
      });
  }

  submit.addEventListener("click", send);
  box.addEventListener("keydown", function (ev) {
    if ((ev.metaKey || ev.ctrlKey) && ev.key === "Enter") send();
  });
})();
