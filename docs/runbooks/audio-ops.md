# Audio narration ops (draft → mp3, daily casts)

**When.** You're operating the narration pipeline itself — running a
one-off `precis draft audio` export, diagnosing why a daily cast didn't
publish, or touching the `cast_audio` service config. Agent-facing voice
score / pronunciation-lexicon usage lives in
`get(kind='skill', id='precis-audio-help')`; this is the host-operator
half.

## Narrate a draft on the CLI

    precis draft audio <slug|id> [--voice af_heart] [--lang en-us]
                                 [--speed 1.0] [--max-segments N] [--publish]

Runs on a host with the `[tts]` extra + Kokoro model files + ffmpeg (the
inference node, `spark`). `--publish` drops the episode on the private
podcast feed (`precis podcast add` / `/podcast/feed.xml`). `--max-segments`
previews a long draft. `speakable()` strips handles/citations/math/markdown
for the ear; math is currently spoken as "equation" (LaTeX→speech is
backlogged).

## Automatic producers

Drafts aren't the only thing that narrates. The **news briefing** used to
publish its own daily episode via a `briefing_audio` worker pass — now
**retired** (`PRECIS_BRIEFING_AUDIO_ENABLED=0`, avoids double-publishing): the
news wire is instead folded into the morning `reading` cast at narration time
(see below), so one combined episode ships. Non-draft producers reuse
`export.audio.synthesize_text` (the shared stitch loop); copy the
`briefing_audio` pass to wire the next one.

## Daily casts — morning brief + evening nidra

Two standing **casts** publish daily, each a `draft` composed then narrated
through the same path — two voice profiles over one spine:

- **`reading`** — the morning situational-awareness brief (voice `bm_george`,
  ~15 min). Producer `reading.briefing_cast.build_reading_briefing` unions
  lanes: overnight system activity, recall (Anki leeches + new concepts), and
  — as they land — reading (booklet) + quest activity. The news wire is no
  longer a compose lane — `cast_audio` prepends the full `briefing-<date>`
  wire (read in the brief's own voice) ahead of the personal brief at
  narration time (below).
- **`nidra`** — the evening concept-graph meditation (voice `af_nicole`, ~45 min).
  Producer `reading.meditation.build_meditation`, a segmented long-form walk.

Both casts **link their draft back to the sources they drew on** (a cast names
its sources but reads no URL aloud, so the link is the durable pointer): the
`reading` brief links papers/findings `cites` and drafts/quests `related-to`;
the `nidra` walk links each walked concept `related-to`. So `links_for` on the
cast draft reopens what it mentioned.

Both compose with a **capable model** (`claude-sonnet-5`, pinned — prose
composition, quota-resilient vs the FRONTIER Opus default) and persist a standalone dated
`draft` marked `meta.cast` (+ `meta.voice`), **filed under a Drive folder** ("Morning
brief" / "Evening meditation") so the text shows up in `/drive` — the Drive row also
links the published mp3 + compiled PDF. **TTS is a separate downstream step:**
the `cast_audio` pass on `spark` (its `service_config` row —
`precis service prio spark cast_audio <n>` / `/categorizers` — plus
`PRECIS_TTS_IMAGE`) narrates any un-narrated cast draft via
`render_narration` → `render_episode` →
the feed (`source="brief"` / `"meditation"`), idempotent on `meta.audio_episode_id`.
For `reading`, narration first **prepends today's full `briefing-<date>` news
wire** (`cast_audio._news_lead_in`, read in the brief's own voice `bm_george`)
ahead of the personal brief, so the two publish as **one combined**
`morning_brief_<date>` episode.
The published mp3 and the on-demand PDF share a human stem — `morning_brief_<date>`
/ `evening_meditation_<date>` — not the internal `cast-*` slug. Compose runs
as the `reading_brief` / `meditation` **`claude_inproc`** job_types on a daily
recurring (`meta.schedule` set) watch — on `melchior`, where the litellm proxy
serving `claude-opus` lives (same host as the news briefing); the compose and
the narration never block each other.

    precis cast run reading            # compose today's morning-brief draft now
    precis cast run nidra --publish    # compose + narrate + publish (on spark)
    precis cast schedule --now         # install the daily watches + cast both now

## Consume over the tailnet

Subscribe **on-device** over Tailscale — Apple Podcasts *Add a Show by URL* or
Downcast → the feed URL. Server-mediated apps (Overcast / Pocket Casts) can't
reach a tailnet feed.
