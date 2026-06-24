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
 * Defined as a plain global so Alpine's `x-data="paperDoc(...)"` can
 * call it (mirrors drafts/detail.html.j2's draftDoc). Loaded `defer`,
 * so it runs before Alpine starts on DOMContentLoaded.
 */
function paperDoc(paperId, citedOrd, hasPdf) {
  return {
    paperId,
    hasPdf,
    sidebarOpen: true,
    tab: 'Navigate',
    // search state
    mode: 'semantic',
    q: '',
    results: [],
    loading: false,
    searched: false,
    activeIdx: -1,
    // toc state
    toc: [],
    tocLoaded: false,
    activeSeg: -1,
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
      // pdf.js find matches against whitespace-normalised page text, so a
      // short distinctive run of words matches more reliably than the
      // whole (multi-line, hyphenated) chunk. Take the first ~10 words.
      const words = (text || '').replace(/\s+/g, ' ').trim().split(' ');
      return words.slice(0, 10).join(' ');
    },

    // ── navigate: search + toc ──────────────────────────────────────
    setMode(m) {
      this.mode = m;
      if (m === 'toc') { if (!this.tocLoaded) this.loadToc(); }
      else { this.$nextTick(() => this.$refs.qbox && this.$refs.qbox.focus()); if (this.q.trim()) this.runSearch(); }
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
    },
    async loadToc() {
      try {
        const data = await (await fetch(`/papers/${this.paperId}/toc`, { cache: 'no-store' })).json();
        this.toc = data.segments || [];
      } catch (e) { this.toc = []; }
      this.tocLoaded = true;
    },
    gotoResult(r, i) {
      this.activeIdx = i;
      this.findInPdf(this._phrase(r.text), r.page);
    },
    gotoSeg(s, i) {
      this.activeSeg = i;
      // A cluster spans many chunks with no single quotable phrase — jump
      // to its first page (highlight the lead keyword as a soft anchor).
      this.findInPdf(s.keywords && s.keywords[0] ? s.keywords[0] : '', s.page);
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
