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
 * The Navigate tab has three modes:
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
 *
 * Defined as a plain global so Alpine's `x-data="paperDoc(...)"` can
 * call it (mirrors drafts/detail.html.j2's draftDoc). Loaded before
 * Alpine starts on DOMContentLoaded.
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
      if (page) app.page = Number(page);
      if (!query) return;
      app.eventBus.dispatch('find', {
        source: null, type: '', query,
        caseSensitive: false, entireWord: false,
        highlightAll: true, findPrevious: false, matchDiacritics: false,
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
      this.findInPdf(s.keywords && s.keywords[0] ? s.keywords[0] : '', s.page);
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
      const ord = this.jord;
      if (ord === '' || ord === null) return;
      try {
        const data = await (await fetch(`/papers/${this.paperId}/chunk/${ord}`, { cache: 'no-store' })).json();
        this.jumpChunk = data.chunk;
      } catch (e) { this.jumpChunk = null; }
      if (this.jumpChunk) {
        this.findInPdf(this._phrase(this.jumpChunk.text), this.jumpChunk.page);
      }
    },
  };
}
