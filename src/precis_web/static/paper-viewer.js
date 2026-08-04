/* Paper detail reader — sidebar nav (semantic / keyword / TOC + jump)
 * driving the vendored pdf.js viewer embedded in an iframe.
 *
 * The viewer is same-origin (served from /static/pdfjs/), so we reach
 * into its `PDFViewerApplication` to jump pages and run its find bar.
 * No bbox data exists per chunk (marker drops it), so "highlight a
 * chunk" works off pdf.js's own text-layer search: we feed it a
 * distinctive phrase from the chunk text and let it highlightAll +
 * scroll. A page jump is the always-correct fallback when the phrase
 * doesn't match (hyphenation / ligatures / math).
 *
 * The Navigate tab has four modes:
 *   - semantic / keyword: a search box over the empty-query "rapid nav"
 *     gloss list (every chunk's llm-v1 summary / keyword string, from
 *     /chunks). A query swaps the list for ranked hits (/search). Either
 *     row: click = jump + highlight; the gloss line clamps and expands on
 *     hover (see .nav-clamp in detail.html.j2).
 *   - toc: keyword-clustered segments (/toc). Single-click = jump +
 *     highlight the cluster's first chunk; double-click = drill into that
 *     cluster (re-cluster its ord range, /toc?lo=&hi=). A breadcrumb +
 *     ↑ climb back out — papers have no heading tree, so hierarchy is
 *     recursive keyword clustering.
 *   - raw: the verbatim chunk-text listing (/rawchunks), no search box —
 *     for a chunks-only doc (no PDF, e.g. a stub-free ingest with nothing
 *     to render on the right) this is the only in-UI way to read the
 *     source text. Click = jump + highlight, same as the other modes.
 *
 * Defined as a plain global so Alpine's `x-data="paperDoc(...)"` can
 * call it (mirrors the classic /drafts reader's draftDoc, since retired).
 * Loaded before Alpine starts on DOMContentLoaded.
 */
function paperDoc(paperId, citedOrd, hasPdf, initialTab) {
  return {
    paperId,
    hasPdf,
    sidebarOpen: true,
    tab: initialTab || 'Navigate',
    // search state
    mode: 'semantic',
    q: '',
    results: [],
    loading: false,
    searched: false,
    activeIdx: -1,
    // rapid-nav gloss list (empty-query state of semantic / keyword)
    chunks: [],
    chunksLoaded: false,
    // raw chunk-text listing (verbatim body, reading order)
    rawChunks: [],
    rawLoaded: false,
    activeRawIdx: -1,
    // toc state
    toc: [],
    tocLoaded: false,
    activeSeg: -1,
    tocStack: [], // drill-down scopes: [{lo, hi}, ...]; [] = whole paper
    _segTimer: null, // click/dblclick discriminator
    // jump state
    jtext: '',
    jpage: '',
    jord: '',
    jumpChunk: null,
    // Small status line surfaced next to the Jump tab: '' when the last
    // find/jump was a clean phrase match, else a note that we landed on
    // an approximate page (or that the entered ord/handle was garbage).
    findStatus: '',
    // Generation counter for _findAndCount — bumped on every call so an
    // older, still-listening call can tell its `updatefindmatchescount`
    // result apart from a newer one's (pdf.js's event carries no query
    // correlation, so two overlapping finds would otherwise cross-talk).
    _findGen: 0,
    // Sources / Cited: server-rendered HTML fragments (unlike every other
    // tab's JSON + client-rendered list), lazy-loaded once per tab per
    // page load — see setTab() / _loadRefs().
    sourcesLoaded: false,
    citedLoaded: false,

    init() {
      // A ?chunk=N citation deep link: land on that chunk (text shown in
      // the Jump panel, highlighted in the PDF) instead of an inline card.
      if (citedOrd >= 0) {
        this.tab = 'Jump';
        this.jord = String(citedOrd);
        this.$nextTick(() => this.jumpOrd());
      }
      // Warm the rapid-nav gloss list if we open straight onto Navigate.
      if (this.tab === 'Navigate' && this.mode !== 'toc') this.loadChunks();
      // A ?tab=Sources/Cited deep link opens straight onto one of the
      // htmx-fragment tabs — same lazy-load setTab() does on click.
      this._loadRefsIfNeeded(this.tab);
    },

    // ── tab switch + Sources/Cited lazy fragment load ────────────────
    setTab(t) {
      this.tab = t;
      this._loadRefsIfNeeded(t);
    },
    _loadRefsIfNeeded(t) {
      if (t === 'Sources' && !this.sourcesLoaded) {
        this.sourcesLoaded = true;
        this._loadRefs('sources', 'refs-sources-panel');
      }
      if (t === 'Cited' && !this.citedLoaded) {
        this.citedLoaded = true;
        this._loadRefs('cited', 'refs-cited-panel');
      }
    },
    // Server-rendered HTML fragment (not JSON) — the row markup (held
    // link vs. off-site links + Fetch form) lives in the Jinja template,
    // not duplicated in JS. htmx.ajax swaps it in and wires up any
    // hx-* attributes the fragment itself carries (the per-row Fetch
    // form); falls back to a plain fetch+innerHTML if htmx isn't loaded.
    _loadRefs(direction, targetId) {
      const url = `/papers/${this.paperId}/refs/${direction}`;
      if (window.htmx) {
        window.htmx.ajax('GET', url, { target: '#' + targetId, swap: 'innerHTML' });
      } else {
        fetch(url, { cache: 'no-store' })
          .then((r) => r.text())
          .then((html) => {
            const el = document.getElementById(targetId);
            if (el) el.innerHTML = html;
          });
      }
    },

    // ── pdf.js viewer control ───────────────────────────────────────
    async _app() {
      if (!this.hasPdf) return null;
      const frame = document.getElementById('pdf-frame');
      if (!frame) return null;
      let app = null;
      for (let i = 0; i < 200; i++) {
        try { app = frame.contentWindow && frame.contentWindow.PDFViewerApplication; }
        catch (e) { app = null; }
        if (app && app.initializedPromise) break;
        await new Promise((r) => setTimeout(r, 100));
      }
      if (!app || !app.initializedPromise) return null;
      await app.initializedPromise;
      if (!app.pdfDocument) {
        await new Promise((res) => {
          app.eventBus.on('pagesloaded', () => res(), { once: true });
          setTimeout(res, 15000);
        });
      }
      return app;
    },
    async gotoPage(n) {
      const app = await this._app();
      if (app && n) app.page = Number(n);
    },
    async findInPdf(query, page) {
      const app = await this._app();
      if (!app) return;
      const phrase = (query || '').trim();
      this.findStatus = '';
      if (!phrase) {
        // No usable phrase at all — the page jump is the only anchor we have.
        if (page) { app.page = Number(page); this.findStatus = '~p.' + page; }
        return;
      }
      // Trust the phrase first: dispatch the find *without* jumping to the
      // (often-wrong) page_first guess, and let pdf.js's own text-layer
      // match position the viewport. Only fall back to the page guess —
      // marked visibly approximate — when the phrase doesn't match.
      const total = await this._findAndCount(app, phrase);
      // A newer findInPdf call started while this one's find was still
      // pending (see _findAndCount) — that call owns findStatus and the
      // page fallback now; this one has nothing more to do.
      if (total === null) return;
      if (total < 1) {
        if (page) {
          app.page = Number(page);
          this.findStatus = 'text not found — jumped to p.' + page + ' (approximate)';
        } else {
          this.findStatus = 'text not found';
        }
      }
    },
    // Dispatch a pdf.js text-layer find and resolve with the settled match
    // total. `updatefindmatchescount` fires progressively as pages are
    // scanned; resolve as soon as it reports >=1 (the viewport is already
    // positioned by then), else time out at 0. Always unsubscribes on the
    // way out, so a repeated call never leaks a listener.
    //
    // pdf.js's `updatefindmatchescount` event carries no query
    // correlation — if a second call starts before the first's listener
    // has unsubscribed (a fresh Jump/nav click within the 1.5s window),
    // both listeners see every event and could resolve each other's
    // promise with the wrong query's count. `_findGen` disambiguates:
    // each call captures the generation at dispatch time, and only
    // resolves a real total if it's still the current generation when a
    // match lands; a superseded call still always unsubscribes (so it
    // never leaks a listener) but resolves `null` — a "stale, not a
    // real total" sentinel `findInPdf` skips (no findStatus write, no
    // page fallback) rather than mistaking for "0 matches".
    _findAndCount(app, query) {
      const gen = ++this._findGen;
      return new Promise((resolve) => {
        let done = false;
        const finish = (total) => {
          if (done) return;
          done = true;
          app.eventBus.off('updatefindmatchescount', onCount);
          resolve(gen === this._findGen ? total : null);
        };
        const onCount = (e) => {
          const total = e && e.matchesCount ? e.matchesCount.total : 0;
          if (total >= 1) finish(total);
        };
        app.eventBus.on('updatefindmatchescount', onCount);
        setTimeout(() => finish(0), 1500);
        app.eventBus.dispatch('find', {
          source: null, type: '', query,
          caseSensitive: false, entireWord: false,
          highlightAll: true, findPrevious: false, matchDiacritics: false,
        });
      });
    },
    _phrase(text) {
      // pdf.js find matches the PDF's *rendered* text layer and needs the
      // whole query to match contiguously. Marker chunk text carries
      // markup the rendered page doesn't have ($d_k$, [3], \alpha), so a
      // naive first-N-words phrase fails the moment it hits one. Pick the
      // first contiguous run of plain alphabetic words (skipping any token
      // with math / citation / symbol chars) — that run exists verbatim on
      // the page. Fall back to the first few raw words if none is found.
      const norm = (text || '').replace(/\s+/g, ' ').trim();
      if (!norm) return '';
      const toks = norm.split(' ');
      const isClean = (t) => /^[A-Za-z][A-Za-z'-]*[.,;:]?$/.test(t) && t.length > 1;
      let best = [], cur = [];
      for (const t of toks) {
        if (isClean(t)) {
          cur.push(t.replace(/[.,;:]$/, ''));
          if (cur.length >= 8) { best = cur; break; }
        } else {
          if (cur.length > best.length) best = cur;
          cur = [];
        }
      }
      if (cur.length > best.length) best = cur;
      const run = best.slice(0, 8);
      return run.length >= 3 ? run.join(' ') : toks.slice(0, 6).join(' ');
    },

    // ── navigate: search + rapid-nav list + toc ─────────────────────
    setMode(m) {
      this.mode = m;
      if (m === 'toc') { if (!this.tocLoaded) this.loadToc(); return; }
      if (m === 'raw') { if (!this.rawLoaded) this.loadRaw(); return; }
      if (!this.chunksLoaded) this.loadChunks();
      this.$nextTick(() => this.$refs.qbox && this.$refs.qbox.focus());
      if (this.q.trim()) this.runSearch();
    },
    // Clearing the box drops back to the rapid-nav gloss list.
    onQueryInput() {
      if (!this.q.trim()) { this.searched = false; this.results = []; this.activeIdx = -1; }
    },
    async loadChunks() {
      try {
        const data = await (await fetch(`/papers/${this.paperId}/chunks`, { cache: 'no-store' })).json();
        this.chunks = data.chunks || [];
      } catch (e) { this.chunks = []; }
      this.chunksLoaded = true;
    },
    // Raw mode's verbatim chunk-text listing — each row already carries
    // its own text, so unlike the gloss list a click needs no follow-up
    // /chunk/<ord> fetch to highlight.
    async loadRaw() {
      try {
        const data = await (await fetch(`/papers/${this.paperId}/rawchunks`, { cache: 'no-store' })).json();
        this.rawChunks = data.chunks || [];
      } catch (e) { this.rawChunks = []; }
      this.rawLoaded = true;
    },
    gotoRaw(r, i, ev) {
      // The row is selectable (so the verbatim body can be copied); a click
      // that ends a text selection is the user copying, not navigating — so
      // don't also jump the PDF in that case.
      const sel = window.getSelection ? window.getSelection().toString() : '';
      if (ev && sel) return;
      this.activeRawIdx = i;
      this.findInPdf(this._phrase(r.text || ''), r.page);
    },
    async runSearch() {
      const q = this.q.trim();
      this.activeIdx = -1;
      if (!q) { this.results = []; this.searched = false; return; }
      this.loading = true;
      try {
        const url = `/papers/${this.paperId}/search?q=${encodeURIComponent(q)}&mode=${this.mode}`;
        const data = await (await fetch(url, { cache: 'no-store' })).json();
        this.results = data.results || [];
        this.mode = data.mode || this.mode; // reflect a semantic→keyword degrade
      } catch (e) { this.results = []; }
      this.loading = false;
      this.searched = true;
      // Surface the best match immediately: jump the PDF to the top hit
      // (cosine-closest for semantic, top ts_rank for keyword) and ring it.
      if (this.results.length) this.gotoNav(this.results[0], 0);
    },
    // The rows the semantic / keyword list shows: ranked hits after a
    // search, else the whole-paper gloss list for rapid nav.
    navRows() {
      return this.searched ? this.results : this.chunks;
    },
    // The one line each row shows: the summary in semantic mode, the
    // keyword string in keyword mode, each falling back to the other
    // (then to a text snippet) so a not-yet-summarised chunk still reads.
    glossText(r) {
      const kw = Array.isArray(r.keywords) ? r.keywords.join(', ') : (r.keywords || '');
      const sum = (r.summary || '').trim();
      const snip = (r.text || '').trim();
      if (this.mode === 'keyword') return kw || sum || snip || '(no keywords yet)';
      return sum || kw || snip || '(no summary yet)';
    },
    async gotoNav(r, i) {
      this.activeIdx = i;
      let text = r.text, page = r.page;
      if (!text) {
        // A gloss-list row carries no chunk text — fetch it to highlight.
        try {
          const d = await (await fetch(`/papers/${this.paperId}/chunk/${r.ord}`, { cache: 'no-store' })).json();
          if (d.chunk) { text = d.chunk.text; page = d.chunk.page || page; }
        } catch (e) { /* page jump is the fallback below */ }
      }
      this.findInPdf(this._phrase(text || ''), page);
    },

    async loadToc(lo, hi) {
      let url = `/papers/${this.paperId}/toc`;
      if (lo !== undefined && hi !== undefined) url += `?lo=${lo}&hi=${hi}`;
      try {
        const data = await (await fetch(url, { cache: 'no-store' })).json();
        this.toc = data.segments || [];
      } catch (e) { this.toc = []; }
      this.tocLoaded = true;
    },
    // Single click vs double click on a TOC row: a click highlights, a
    // double-click drills in. Defer the single-click action briefly so a
    // double-click can cancel it.
    onSegClick(s, i) {
      if (this._segTimer) clearTimeout(this._segTimer);
      this._segTimer = setTimeout(() => { this._segTimer = null; this.gotoSeg(s, i); }, 220);
    },
    onSegDblClick(s) {
      if (this._segTimer) { clearTimeout(this._segTimer); this._segTimer = null; }
      this.drillSeg(s);
    },
    async gotoSeg(s, i) {
      this.activeSeg = i;
      // Highlight the cluster's first chunk (its opening phrase) — the
      // same green find as a Jump. Fall back to the lead keyword if the
      // chunk text can't be fetched.
      try {
        const d = await (await fetch(`/papers/${this.paperId}/chunk/${s.lo}`, { cache: 'no-store' })).json();
        if (d.chunk) { this.findInPdf(this._phrase(d.chunk.text), d.chunk.page || s.page); return; }
      } catch (e) { /* fall through */ }
      // No chunk text to fetch — fall back to the segment's own label. A
      // single stemmed KeyBERT keyword rarely matches the PDF's rendered
      // text layer verbatim (the red pdf.js notFound bar); running the
      // joined keyword phrase through the same clean-word extraction as a
      // chunk's text gives the find a real multi-word run to try.
      const label = (s.keywords || []).join(' ');
      this.findInPdf(this._phrase(label), s.page);
    },
    // Drill into a multi-chunk cluster: push its ord range and re-cluster.
    // A single-chunk row (lo === hi) has nothing finer to show.
    drillSeg(s) {
      if (s.lo === s.hi) return;
      this.tocStack.push({ lo: s.lo, hi: s.hi });
      this.activeSeg = -1;
      this.loadToc(s.lo, s.hi);
    },
    tocUp() {
      if (!this.tocStack.length) return;
      this.tocStack.pop();
      this.activeSeg = -1;
      const sc = this.tocStack[this.tocStack.length - 1];
      if (sc) this.loadToc(sc.lo, sc.hi); else this.loadToc();
    },
    tocReset() {
      this.tocStack = [];
      this.activeSeg = -1;
      this.loadToc();
    },
    tocPopTo(k) {
      this.tocStack = this.tocStack.slice(0, k + 1);
      this.activeSeg = -1;
      const sc = this.tocStack[k];
      this.loadToc(sc.lo, sc.hi);
    },

    // ── jump: text / page / ord ─────────────────────────────────────
    jumpText() {
      const t = this.jtext.trim();
      if (t) this.findInPdf(t, null);
    },
    jumpPage() {
      if (this.jpage) this.gotoPage(this.jpage);
    },
    async jumpOrd() {
      const raw = (this.jord || '').trim();
      if (!raw) return;
      // Accept the same compound handles the TOC displays (`pa<id>~lo..hi`):
      // strip the prefix and take the low end of a range client-side, so a
      // handle pasted straight out of the TOC works here too instead of
      // silently doing nothing (the box used to be `type="number"`, which
      // couldn't even accept the ``~``/``..`` characters).
      const m = raw.match(/^(?:pa\d+~)?(\d+)(?:\.\.\d+)?$/);
      if (!m) {
        this.jumpChunk = null;
        this.findStatus = '"' + raw + '" isn\'t a chunk number — paste "N" or a TOC handle';
        return;
      }
      const ord = m[1];
      try {
        const data = await (await fetch(`/papers/${this.paperId}/chunk/${ord}`, { cache: 'no-store' })).json();
        this.jumpChunk = data.chunk;
      } catch (e) { this.jumpChunk = null; }
      if (this.jumpChunk) {
        this.findStatus = '';
        this.findInPdf(this._phrase(this.jumpChunk.text), this.jumpChunk.page);
      } else {
        this.findStatus = 'chunk ' + ord + ' not found';
      }
    },
  };
}
