// In-app guided tour overlay (docs/backlog/user-guide-demo.md, slice 1).
//
// Activated by ?tour=<slug> in the URL on ANY page (loaded from
// base.html.j2, so it runs everywhere). Fetches the matching manifest from
// GET /manual/tour/<slug>.json (routes/manual.py) and, if found, renders a
// dimmed backdrop + a highlight ring around the current step's
// `[data-tour="<anchor>"]` element + an arrowed callout card.
//
// Deliberately dependency-free: htmx/Alpine are loaded `defer` in <head> and
// may not have run yet by DOMContentLoaded, and a page with no manifest
// (the overwhelming common case) must pay nothing beyond one fetch. No
// Alpine component, no htmx wiring — plain DOM + fetch.
//
// `&step=N` (1-based, indexes the manifest's own `steps` array — not a
// filtered "visible steps" list) deep-links a specific step; Next/Prev
// rewrite `step` via history.replaceState (never pushState, so the tour
// doesn't flood the back-button history). This N-of-manifest indexing is
// deliberate: the (future) capture pipeline drives the page with
// `?tour=X&step=N` for a fixed N and expects a deterministic render for
// that exact N — an anchor missing on a particular page/step is a capture-
// time failure to surface, not something tour.js should paper over by
// silently renumbering steps out from under a fixed URL.
//
// A step whose anchor isn't on the page (a moved/renamed element — UI
// drift) still renders its callout card (heading/text/counter), just
// without a highlight ring or scroll, and logs one console.warn. Next/Prev
// additionally auto-skip PAST such a step when walking (bounded by the
// first/last step), so a mid-tour drift doesn't strand the visitor on a
// dead step — but a direct ?step=N deep link always renders exactly N.
(function () {
  "use strict";

  function currentTourParams() {
    const usp = new URLSearchParams(window.location.search);
    const tour = usp.get("tour");
    if (!tour) return null;
    const stepRaw = parseInt(usp.get("step"), 10);
    const step = Number.isFinite(stepRaw) && stepRaw > 0 ? stepRaw : 1;
    return { tour, step };
  }

  function injectStyle() {
    if (document.getElementById("precis-tour-style")) return;
    const style = document.createElement("style");
    style.id = "precis-tour-style";
    style.textContent = `
      #precis-tour-backdrop {
        position: fixed; inset: 0; z-index: 9990;
        background: rgba(15, 23, 42, 0.55);
        pointer-events: none;
      }
      #precis-tour-ring {
        position: fixed; z-index: 9991;
        border: 2px solid #0ea5e9;
        border-radius: 6px;
        box-shadow: 0 0 0 4px rgba(14, 165, 233, 0.25), 0 0 0 9999px rgba(15, 23, 42, 0.55);
        pointer-events: none;
        transition: top .15s ease, left .15s ease, width .15s ease, height .15s ease;
      }
      #precis-tour-card {
        position: fixed; z-index: 9992;
        width: 300px; max-width: calc(100vw - 24px);
        background: #ffffff;
        border: 1px solid #e2e8f0;
        border-radius: 8px;
        box-shadow: 0 10px 30px rgba(15, 23, 42, 0.35);
        padding: 12px 14px;
        font: 13px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        color: #334155;
        pointer-events: auto;
      }
      #precis-tour-card h3 {
        margin: 0 0 6px; font-size: 14px; font-weight: 600; color: #0f172a;
      }
      #precis-tour-card p { margin: 0 0 10px; }
      #precis-tour-card .precis-tour-foot {
        display: flex; align-items: center; gap: 8px; font-size: 11px; color: #64748b;
      }
      #precis-tour-card .precis-tour-foot .precis-tour-count { margin-right: auto; }
      #precis-tour-card button {
        font: inherit; font-size: 12px; font-weight: 500;
        border: 1px solid #cbd5e1; border-radius: 5px;
        background: #f8fafc; color: #334155;
        padding: 3px 9px; cursor: pointer;
      }
      #precis-tour-card button:hover { background: #f1f5f9; }
      #precis-tour-card button.precis-tour-primary {
        background: #0284c7; border-color: #0284c7; color: #fff;
      }
      #precis-tour-card button.precis-tour-primary:hover { background: #0369a1; }
      #precis-tour-card button:disabled { opacity: .4; cursor: default; }
      #precis-tour-card button:disabled:hover { background: #f8fafc; }
    `;
    document.head.appendChild(style);
  }

  function TourController(manifest, tourSlug) {
    const steps = Array.isArray(manifest.steps) ? manifest.steps : [];
    let backdrop = null;
    let ring = null;
    let card = null;

    function mount() {
      backdrop = document.createElement("div");
      backdrop.id = "precis-tour-backdrop";
      ring = document.createElement("div");
      ring.id = "precis-tour-ring";
      ring.style.display = "none";
      card = document.createElement("div");
      card.id = "precis-tour-card";
      document.body.appendChild(backdrop);
      document.body.appendChild(ring);
      document.body.appendChild(card);
    }

    function unmount() {
      [backdrop, ring, card].forEach((el) => el && el.remove());
      backdrop = ring = card = null;
      document.removeEventListener("keydown", onKeydown);
      const url = new URL(window.location.href);
      url.searchParams.delete("tour");
      url.searchParams.delete("step");
      history.replaceState(null, "", url);
    }

    function setStepParam(n) {
      const url = new URL(window.location.href);
      url.searchParams.set("tour", tourSlug);
      url.searchParams.set("step", String(n));
      history.replaceState(null, "", url);
    }

    function anchorEl(step) {
      return step && step.anchor
        ? document.querySelector('[data-tour="' + step.anchor + '"]')
        : null;
    }

    // Position the callout card relative to a highlighted element's rect,
    // per the step's `placement`; falls back to viewport-centred when there
    // is no rect (anchor missing on this page).
    function placeCard(rect, placement) {
      const margin = 12;
      const cw = card.offsetWidth || 300;
      const ch = card.offsetHeight || 120;
      let top;
      let left;
      if (!rect) {
        top = Math.max(margin, (window.innerHeight - ch) / 2);
        left = Math.max(margin, (window.innerWidth - cw) / 2);
      } else {
        switch (placement) {
          case "top":
            top = rect.top - ch - margin;
            left = rect.left + rect.width / 2 - cw / 2;
            break;
          case "left":
            top = rect.top + rect.height / 2 - ch / 2;
            left = rect.left - cw - margin;
            break;
          case "right":
            top = rect.top + rect.height / 2 - ch / 2;
            left = rect.right + margin;
            break;
          case "bottom":
          default:
            top = rect.bottom + margin;
            left = rect.left + rect.width / 2 - cw / 2;
            break;
        }
      }
      top = Math.min(Math.max(margin, top), window.innerHeight - ch - margin);
      left = Math.min(Math.max(margin, left), window.innerWidth - cw - margin);
      card.style.top = top + "px";
      card.style.left = left + "px";
    }

    function render(n) {
      const total = steps.length;
      const idx = Math.min(Math.max(n, 1), total) - 1;
      const step = steps[idx];
      setStepParam(idx + 1);

      const el = anchorEl(step);
      if (!el) {
        console.warn(
          "precis tour: step " + (idx + 1) + " ('" + tourSlug + "') anchor " +
          '"' + (step && step.anchor) + '" not found on this page — skipping highlight (UI drift?)'
        );
      } else {
        el.scrollIntoView({ block: "center", inline: "nearest" });
      }

      // Card content is rebuilt BEFORE it's positioned, and the ring/card
      // placement itself is deferred one frame — both so `placeCard` reads
      // the card's REAL (post-content) size instead of a guess, and so an
      // (instant) scroll from the branch above has settled before anything
      // measures the anchor's rect.
      card.innerHTML = "";
      const h = document.createElement("h3");
      h.textContent = (step && step.heading) || manifest.title || "";
      const p = document.createElement("p");
      p.textContent = (step && step.text) || "";
      const foot = document.createElement("div");
      foot.className = "precis-tour-foot";
      const count = document.createElement("span");
      count.className = "precis-tour-count";
      count.textContent = (idx + 1) + " of " + total;
      const prevBtn = document.createElement("button");
      prevBtn.type = "button";
      prevBtn.textContent = "Prev";
      prevBtn.disabled = idx === 0;
      prevBtn.addEventListener("click", () => go(idx, -1));
      const nextBtn = document.createElement("button");
      nextBtn.type = "button";
      nextBtn.className = "precis-tour-primary";
      nextBtn.textContent = idx + 1 === total ? "Done" : "Next";
      nextBtn.addEventListener("click", () => {
        if (idx + 1 === total) unmount();
        else go(idx, 1);
      });
      foot.appendChild(count);
      foot.appendChild(prevBtn);
      foot.appendChild(nextBtn);
      card.appendChild(h);
      card.appendChild(p);
      card.appendChild(foot);

      requestAnimationFrame(() => {
        if (!el) {
          ring.style.display = "none";
          placeCard(null, step && step.placement);
          return;
        }
        const rect = el.getBoundingClientRect();
        ring.style.display = "block";
        ring.style.top = rect.top - 4 + "px";
        ring.style.left = rect.left - 4 + "px";
        ring.style.width = rect.width + 8 + "px";
        ring.style.height = rect.height + 8 + "px";
        placeCard(rect, step && step.placement);
      });
    }

    // Move `dir` steps (±1) from 1-based `fromIdx1`, auto-skipping any step
    // whose anchor is missing (bounded by the ends — never wraps and never
    // walks fully off the manifest even if every remaining anchor is gone).
    function go(fromIdx0, dir) {
      let next = fromIdx0 + 1 + dir; // 1-based target
      while (next >= 1 && next <= steps.length && !anchorEl(steps[next - 1])) {
        console.warn(
          'precis tour: step ' + next + " ('" + tourSlug + "') anchor missing — skipping"
        );
        next += dir;
      }
      if (next < 1 || next > steps.length) {
        // Nowhere valid to land — just re-render the step we started from.
        render(fromIdx0 + 1);
        return;
      }
      render(next);
    }

    function onKeydown(e) {
      if (e.key === "Escape") {
        unmount();
      } else if (e.key === "ArrowRight") {
        const cur = parseInt(new URLSearchParams(window.location.search).get("step"), 10) || 1;
        go(cur - 1, 1);
      } else if (e.key === "ArrowLeft") {
        const cur = parseInt(new URLSearchParams(window.location.search).get("step"), 10) || 1;
        go(cur - 1, -1);
      }
    }

    this.start = function (initialStep) {
      if (!steps.length) return; // nothing to show — no-op, no overlay
      mount();
      document.addEventListener("keydown", onKeydown);
      render(initialStep);
    };
  }

  document.addEventListener("DOMContentLoaded", function () {
    const params = currentTourParams();
    if (!params) return;

    fetch("/manual/tour/" + encodeURIComponent(params.tour) + ".json")
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error("http " + r.status))))
      .then((manifest) => {
        injectStyle();
        new TourController(manifest, params.tour).start(params.step);
      })
      .catch(() => {
        // Unknown slug, network error, malformed JSON, 500 from a broken
        // manifest — all of it is a silent no-op. A page without a tour
        // (or with a tour the server can't currently serve) must render
        // exactly as if this script weren't here at all.
      });
  });
})();
