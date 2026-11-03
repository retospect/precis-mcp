// Prefetch/render cache for the smartdraft reader (gr553319).
//
// Arrow-stepping used to block on a full-page fetch before the next
// block showed as focused — each `?focus=` nav re-rendered the whole
// `#sd-content` panel server-side and the user waited on it. This module
// owns the client policy that makes stepping feel instant:
//
//   entries   — an LRU (default 20) of `#sd-content` renders keyed by
//     page URL, each carrying the `v` render token `#sd-state` ships so
//     a hit can be told apart from a stale copy without a byte-compare
//     (the header panel renders relative timestamps, so identical HTML
//     twice is not guaranteed). `stale` flags an entry a revalidate
//     already proved old but couldn't swap in (an open editor was in
//     the way) — hits skip it and the next nav refetches.
//   inflight  — url -> {ctrl, promise, ep, pinned}: one shared in-flight
//     fetch per URL, so a real nav to a URL mid-prefetch just awaits it
//     (and upgrades it to `pinned`: reap()/clear() never abort a fetch a
//     real navigation is waiting on — that turned a jump or an edit
//     during a nav into a full location.href reload of a page the user
//     had already left).
//   wanted    — the ±`radius` window around the focus in reading order
//     (`ST.order`), plus ±`radius` in `ST.hits` while a search is on —
//     the set of pages worth prefetching after each swap.
//   reap      — aborts every UNPINNED in-flight fetch outside a keep
//     set: a jump cancels the stale window's prefetches.
//   clear     — drops everything + bumps an epoch, so a fetch that
//     resolves after a mutation-refresh can't re-add a pre-write copy
//     (it resolves 'stale' instead of 'ok'; pinned fetches keep running
//     for their awaiter but their result is never applied or cached).
//
// Pure data + AbortController — no DOM, no fetch — so node can test it;
// view.html.j2 supplies the fetching (`fetchPanel`) and DOM halves.

export function createRenderCache({ max = 20, radius = 3 } = {}) {
  const entries = new Map(); // url -> {html, v, focus, focusHandle, stale?}
  const inflight = new Map(); // url -> {ctrl, promise, ep, pinned}
  let epoch = 0;

  // LRU read — a hit refreshes the entry to newest.
  function get(url) {
    const e = entries.get(url);
    if (e) {
      entries.delete(url);
      entries.set(url, e);
    }
    return e;
  }

  function has(url) {
    return entries.has(url);
  }

  // Only versioned renders are cacheable: without `v` a hit couldn't be
  // revalidated (the FakeStore path emits `v: ""` — the pre-cache
  // behaviour is strictly safer than a forever-stale copy there).
  function put(url, entry) {
    if (!entry || !entry.v) return false;
    entries.delete(url);
    entries.set(url, entry);
    while (entries.size > max) entries.delete(entries.keys().next().value);
    return true;
  }

  // Flag an entry known-behind: a background revalidate fetched a moved
  // render but couldn't swap it in (open inline editor). The hit path
  // refuses stale entries, so the next nav refetches instead of serving
  // markup already proven old.
  function markStale(url) {
    const e = entries.get(url);
    if (e) e.stale = true;
  }

  // Drop every cached render FOCUSED on `handle` — the `draft:*` editor
  // events carry the block's base58 (each entry's `focusHandle`, with
  // `focus` matching the dc form too, belt and braces). Neighbouring
  // cached pages that merely SHOW the edited block go stale via their
  // own `v` on the next revalidate.
  function dropFocusHandle(handle) {
    if (!handle) return 0;
    let n = 0;
    for (const [url, e] of entries) {
      if (e.focusHandle === handle || e.focus === handle) {
        entries.delete(url);
        n++;
      }
    }
    return n;
  }

  // The post-mutation reset: every cached render predates the write, and
  // an in-flight prefetch's resolve would be a pre-write copy — so abort
  // the unpinned ones and bump the epoch, which turns every straggler's
  // (and any surviving pinned nav fetch's) result 'stale': never cached,
  // never applied.
  function clear() {
    for (const [url, it] of inflight) {
      if (it.pinned) continue;
      it.ctrl.abort();
      inflight.delete(url);
    }
    entries.clear();
    epoch++;
  }

  // One shared fetch per URL: prefetch, real nav and background
  // revalidate all ride the same in-flight request when they overlap.
  // `fetcher(url, signal)` resolves to a cacheable entry ({html, v, …})
  // or null. Awaiters get {status, entry}:
  //   ok       — the fetch settled; `entry` may still be null (the
  //              fetcher declined, e.g. a non-200 → callers may fall back
  //              to a full page load).
  //   stale    — resolved, but clear() moved the epoch under it: refused
  //              into the cache and NOT for applying (a post-mutation
  //              refresh owns the screen).
  //   aborted  — reap/clear killed it. NOT an error: a real nav awaiting
  //              it must never treat this as a fetch failure (that was
  //              the jump-during-load → location.href reload bug).
  //   error    — the fetcher rejected (network down, parse blew up).
  // `opts.pinned` marks a fetch a real navigation is awaiting — reap()
  // and clear() leave it alone; attaching to an existing in-flight
  // request upgrades it in place.
  function track(url, fetcher, opts) {
    const cur = inflight.get(url);
    if (cur && cur.ep === epoch) {
      if (opts && opts.pinned) cur.pinned = true;
      return cur.promise;
    }
    // A pre-clear in-flight request may have been rendered pre-write —
    // a post-mutation caller must not share it. Abort the unpinned
    // straggler; a pinned one keeps running for its own awaiter (which
    // sees 'stale') and this call gets a fresh fetch.
    if (cur && !cur.pinned) cur.ctrl.abort();
    const ctrl = new AbortController();
    const ep = epoch;
    const promise = Promise.resolve()
      .then(() => fetcher(url, ctrl.signal))
      .then((entry) => ({
        // aborted > stale > ok: a resolve after abort is nobody's result;
        // a resolve across a clear() is untrusted; otherwise good.
        status: ctrl.signal.aborted ? 'aborted' : ep === epoch ? 'ok' : 'stale',
        entry: entry || null,
      }))
      .catch((e) => ({
        status: ctrl.signal.aborted || (e && e.name === 'AbortError') ? 'aborted' : 'error',
        entry: null,
      }))
      .then((r) => {
        // An aborted fetch that resolved anyway still carries a good
        // versioned render — reap saves bandwidth, not bytes arrived.
        if (r.entry && ep === epoch) put(url, r.entry);
        return r;
      })
      .finally(() => {
        const it = inflight.get(url);
        if (it && it.ctrl === ctrl) inflight.delete(url);
      });
    inflight.set(url, { ctrl, promise, ep, pinned: !!(opts && opts.pinned) });
    return promise;
  }

  function inflightFor(url) {
    const it = inflight.get(url);
    return it ? it.promise : null;
  }

  // The URLs worth prefetching: ±`radius` around `focus` in `order`
  // (the [[dc, depth], …] nav sequence — same stepping the arrow keys
  // take), and ±`radius` around it in `hits` while a search is active.
  // `urlFor(dc)` builds the page URL (current params + focus=dc).
  function wanted(focus, order, hits, urlFor) {
    const urls = new Set();
    if (!focus) return urls;
    const i = (order || []).findIndex((o) => o[0] === focus);
    if (i >= 0) {
      for (
        let k = Math.max(0, i - radius);
        k <= Math.min(order.length - 1, i + radius);
        k++
      ) {
        urls.add(urlFor(order[k][0]));
      }
    }
    const hs = hits || [];
    const hi = hs.indexOf(focus);
    if (hi >= 0) {
      for (
        let k = Math.max(0, hi - radius);
        k <= Math.min(hs.length - 1, hi + radius);
        k++
      ) {
        urls.add(urlFor(hs[k]));
      }
    }
    return urls;
  }

  // Abort every UNPINNED in-flight fetch whose URL is not in `keep` —
  // the stale window's prefetches after a jump. A pinned fetch (a real
  // nav awaiting it) is never touched. Returns the aborted count.
  function reap(keep) {
    let n = 0;
    for (const [url, it] of inflight) {
      if (!keep.has(url) && !it.pinned) {
        it.ctrl.abort();
        inflight.delete(url);
        n++;
      }
    }
    return n;
  }

  return {
    get,
    has,
    put,
    markStale,
    dropFocusHandle,
    clear,
    track,
    inflightFor,
    wanted,
    reap,
    get size() {
      return entries.size;
    },
    get inflightCount() {
      return inflight.size;
    },
  };
}
