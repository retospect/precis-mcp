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
  var clicked = [];

  function insertHandle(handle) {
    var v = box.value;
    var start = box.selectionStart == null ? v.length : box.selectionStart;
    var end = box.selectionEnd == null ? start : box.selectionEnd;
    var before = v.slice(0, start);
    var after = v.slice(end);
    var lead = before && !/\s$/.test(before) ? " " : "";
    var trail = after && !/^\s/.test(after) ? " " : " ";
    var ins = lead + handle + trail;
    box.value = before + ins + after;
    var caret = start + ins.length;
    box.setSelectionRange(caret, caret);
    box.focus();
    if (clicked.indexOf(handle) === -1) clicked.push(handle);
  }

  function handleAt(doc, ev) {
    var els = doc.elementsFromPoint
      ? doc.elementsFromPoint(ev.clientX, ev.clientY)
      : [ev.target];
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
      var h = handleAt(doc, ev);
      if (h) insertHandle(h);
    });
  }

  var obj = document.getElementById("pcb-board");
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
      showError("type an argument first");
      return;
    }
    errorEl.classList.add("hidden");
    status.textContent = "asking the model…";
    submit.disabled = true;
    // Only handles still present in the text count as clicked.
    var present = clicked.filter(function (h) {
      return text.indexOf(h) !== -1;
    });
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
