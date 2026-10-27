---
status: draft
title: youtube kind — opt-in one-hop recommendation list (gr519600)
pillar: platform
---

# youtube kind — opt-in one-hop recommendation list

Spec for gr519600, which requires a written spec and human review before
implementation (same posture as gr519587). This file IS the spec; nothing
in `src/` changes under it until reviewed.

## Motivation / why

`get(kind='youtube', id=...)` fetches a transcript. The ask: optionally
also surface the video's recommended/related list so an agent can browse
"what's adjacent" for tracking a field without hand-feeding links.

The recommendation graph is an attacker-shaped surface: recommendations
are adversarially optimized (engagement-bait drift, topic hijack), and
rec titles/descriptions are prompt-injection carriers — transcripts
already take the tier-0 injection scan (`_stamp_inject_scan`,
`docs/backlog/untrusted-input-injection-scan.md`). The spec therefore
fixes fetch-only, depth-one, scanned, capped, provenance-tagged
semantics before any code lands.

## Source (verified shape — no new request, no paid API)

Recommendations are NOT in `youtube_transcript_api`; they live in the
watch page's `ytInitialData` JSON — the same page
`_scrape_watch_page_meta` already GETs on every cold fetch
(`src/precis/handlers/youtube.py`), so the feature pays **zero added
wire cost**: parse `ytInitialData.contents.twoColumnWatchNextResults
.secondaryResults.secondaryResults.results[*].compactVideoRenderer`
in the same response (`videoId`, `title`, `shortBylineText` channel,
`lengthText`, `viewCountText`). No paid YouTube Data API, no extra
endpoint, no new dependency — the paid/scrape-blocked answer the gripe
allowed a skip for does not apply.

## In scope

- Opt-in surface — `get(kind='youtube', id=<v>, args={'recommendations':
  true})` (decide vs `view='recommendations'` in review — see open
  questions). Off by default: a plain get changes nothing — nothing
  extra parsed, stored or rendered.
- Render ≤10 recs: video id, title, channel, duration, view count —
  each labelled untrusted external data; the block carries the same
  attribution line as the transcript.
- Injection ladder applied to rec titles + channel names before they
  reach model context — `scan_tier0` per item: a `high` verdict
  withholds that item (never the whole list), `suspect` renders with
  the flagged marker. Same posture as the transcript body.
- Snapshot stored on `cache_state.meta['recommendations']` =
  `{fetched_at, items}` — provenance untrusted, same as the transcript;
  `no_fetch` reads on a warm cache serve the stored list at zero
  network cost.
- Rate/volume: piggybacks the one watch-page GET a cold fetch already
  makes (paced `youtube-scrape` limiter row); a recs read on a warm
  cache costs no request at all — this cannot quietly become a
  crawler.
- Depth one, period: surfacing a rec means listing it; opening one is
  an explicit separate `get` on its id by the caller. No traversal
  helper, no bulk mode, no worker.

## Explicitly NOT in scope

- Auto-following / chaining / multi-hop recommendation walking — the
  drift surface the gripe calls out; no API shape may express it.
- Rec descriptions/transcripts (title/channel only — less attack
  surface, fewer tokens).
- Paid YouTube Data API or any additional endpoint.
- Migration: recs ride `cache_state.meta` (jsonb), no schema change.

## Acceptance criteria

- A plain get on a cold video issues the same one watch-page request it
  does today; the opt-in changes only what is parsed/stored/rendered.
- Every rendered rec passes the tier-0 injection scan; a `high`
  title withholds that one item with the warning marker.
- ≤10 items, ~120-char titles — the block stays small
  (<~2k chars).
- `args={'recommendations': true}` on a warm cached video serves the
  stored snapshot with zero network.
- Tests use canned `ytInitialData` fixtures — never live network.
- A fresh human `get` of a rec is an explicit call — nothing in the
  render chains another fetch.

## Target + blast radius

- `src/precis/handlers/youtube.py` — `_fetch` (meta),
  `_scrape_watch_page_meta` (one extra `ytInitialData` parse on the
  same response), `_render` / a small recs view; `precis.utils
  .inject_scan` reuse. Dispatch extras gate for the new `args` key.
- No migrations, no new outbound surface, no workers.

## Open questions / decisions log

- Surface: `args={'recommendations': true}` vs `view='recommendations'`?
  The arg rides the normal fetch so a cold call returns transcript +
  recs together; a view on a cold ref must either fetch anyway (two
  renders' worth) or refuse. Lean arg — decide in review.
- Refresh semantics: recs drift faster than transcripts — snapshot is
  stamped at fetch time and only re-fetched on an explicit `refresh`,
  not on the 30-day transcript TTL roll. Confirm.
- Whether the `/recent` listing should badge videos that have a recs
  snapshot (cheap meta flag vs nothing — lean nothing).
