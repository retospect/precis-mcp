DREAM CYCLE — execute these steps in order. No user is reading; this is housekeeping for your own continuity. The goal is fact-anchored speculative connections, not introspection.

If a `## This cycle's lens` block precedes this one, adopt that stance throughout — it colours *how* you sample, read, and connect (and, for a process lens, in what order), not *which* steps you run.

Step 1 — diverse self-sample (NOT just recent). Pull a salience-seeded diverse-cone sample of your own internal-thought memories — this uses the rotating-vector-space dreamable view so you actually surface old/unrelated/unfinished thoughts, not just the last thing you wrote:
  precis search(kind="memory", view="dreamable", n=10)
If that returns nothing useful, fall back to:
  precis search(kind="memory", tags=["internal-thought"], page_size=3)

Step 2 — diverse paper sample (the heart of the dream). Pull a salience-seeded diverse-cone sample of papers from the library — this uses the rotating-vector-space dreamable view, not just "recent":
  precis search(kind="paper", view="dreamable", n=12)
If that returns nothing useful, fall back to:
  precis search(kind="paper", q="<a single word from step 1>", page_size=5)

Step 3 — read inside one paper. Pick ONE paper from step 2 that you don't recognise, get its full body so you actually have substance to connect, not just a title:
  precis get(kind="paper", id="<slug from step 2>", view="toc")
then if interesting:
  precis get(kind="paper", id="<slug>")     # full chunks

Step 4 — one more outside-self anchor. Pull one fact-bearing ref from a non-paper kind so the connection can cross domains. ANY of these is fair game (and you may use more than one):
  precis search(kind="patent", view="dreamable", n=5)        # patent corpus
  precis search(kind="patent", q="<a word from step 2>")     # patent text search
  precis search(kind="oracle", q="<a word from step 2>")     # roll-the-dice fact
  precis search(kind="perplexity-research", q="<a word from step 2>")  # cached deep-research
  precis search(kind="web", q="<a word from step 2>")        # cached web pages
  precis get(kind="websearch", id="<query>")                 # live web search via Perplexity
  precis get(kind="web", id="<URL>")                         # live web fetch of a specific page
Web searches and fetches ARE allowed in dreams — the only disallowed tools are Claude's built-in WebFetch/WebSearch; the precis ``web`` and ``websearch`` kinds go through cached/sourced fetches and are perfectly fine.

Step 5 — light conversational signal (2 chunks, ceiling). Optional — only if relevant:
  precis search(kind="conv", q="<a theme from step 2>", page_size=2)

Step 6 — find AT LEAST TWO non-obvious connections. Each new memory MUST link refs of at least 2 different kinds, AND at least one of those refs MUST be a fact-bearing kind (paper / patent / oracle / perplexity-research). A memory↔memory or memory↔conv connection by itself is not acceptable — those are too cheap and self-referential. For each call:

  precis put(
      kind="memory",
      text="I notice <specific connection that goes through a fact>. <One sentence on why this is non-obvious or what it suggests.>",
      title="<short, concise summary of the connection — the scannable header>",
      tags=["DREAM:speculative"]
  )

TITLE DISCIPLINE: write the body first (``text=`` — the full "I notice…" connection), then set ``title=`` once the connection's point is clear. The title is the memory's entire scannable surface — it is what shows in /recent listings, search hits, and the /refs/memory grid. Make it a SHORT, concise summary of the dream connection (the conclusion, not the topic) — ideally under ~12 words, no leading "#" heading, no "I notice…". The body carries the full prose. (If you omit ``title=``, one is derived from the body's first line — but an explicit title reads better, so write one.)

  ❌ title="Free energy and clamp circuits"                        (a topic, not the conclusion)
  ✅ title="Free-energy bound ≈ [pt913]'s clamp circuit"
     text="I notice [pa812]'s free-energy bound mirrors [pt913]'s clamp circuit, which suggests…"

The body MUST name each source ref inline by the `[handle]` the tool result printed for it, so the connection auto-links and stays traceable (`get(kind='skill', id='precis-addressing-help')` for the form). Vague connections are useless; be specific about what links to what AND name the fact-bearing leg explicitly.

DEFINE YOUR ABBREVIATIONS: a memory has no glossary, so spell out each abbreviation on first use in the body — write `AGNR (armchair graphene nanoribbon)`, not a bare `AGNR`. This covers all-caps acronyms and hyphenated compounds (`GNR-FET`).

Step 6b — request papers you'd love to have. If during Step 6 you reference a paper you don't actually hold (a reference cited in something you read but not yet in the library, a paper that would clinch the connection if you had it), request it. One ``put`` mints a stub the fetch_oa worker then chases for an open-access PDF:

  precis put(kind="paper", doi="<DOI>")                       # best — a resolvable id auto-fetches
  precis put(kind="paper", arxiv="<id>", title="<title>")     # or an arXiv id
  precis put(kind="paper", title="<best-known title>")        # title-only backlog stub (no auto-fetch)

A DOI or arXiv id is strongly preferred: it auto-fetches and won't be rejected as a hallucination. A title alone just parks the request in the backlog for manual ingest. Minting is idempotent — re-requesting a paper already held or already wanted is a no-op. This is encouraged: a dream that surfaces 2-3 paper-shaped gaps in the library is doing useful work even before the connections it draws.

  precis search(kind="paper", view="stubs", n=10)             # current backlog — check first so you don't double-spawn
  precis get(kind="skill", id="precis-stubs-help")            # how requests + the chase work

Step 7 — review your own work. For EACH memory you wrote in Step 6 (the put() tool result printed an id, e.g. ``created memory id=34468``), do this check:

  precis get(kind="memory", id=<id>)               # read what you just wrote
  precis get(id="<handle from text>")              # verify each cited handle resolves (e.g. id="pa1234")
  precis get(id="<handle from text>")              # likewise for every patent / oracle / conv handle

For a memory to count as a *synthetic insight* (not just a vague speculative jot), ALL of these must hold:
  * every cited ref id actually resolves (no 404s — but stubs created in Step 6b DO count as resolving, since the worker will fetch them),
  * at least one cited ref is a fact-bearing kind (paper / patent / oracle / perplexity-research / web),
  * the connection text names a SPECIFIC mechanism, not "X and Y are both about Z" abstractions.

You may *also* run web/patent/perplexity searches during Step 7 to verify a cited claim — the same fan-out from Step 4 is allowed here. A memory that was a hunch and turned out to be wrong is still useful as a hunch — leave it tagged ``tier:dream`` (no promotion) and add a brief follow-up memory explaining what didn't check out.

If all three hold, promote the memory:
  precis tag(kind="memory", id=<id>, add=["tier:synthetic-insight"])

If a memory fails verification (cited ref doesn't exist, or is purely memory↔memory, or the connection is vacuous), leave it as ``tier:dream`` only — those stay accessible but won't show up in the operator's curated insights view.

Step 8 — end the session. Do not write a summary message. The tool calls in Step 6 + Step 7 are the deliverable.

REQUIREMENT: You must produce at least two precis put() calls with the DREAM:speculative tag, then run the Step-7 review on each. Each memory must cite ≥2 ref ids of different kinds with ≥1 fact-bearing kind. If you cannot find such connections, say so as a single memory: put(kind="memory", text="Dream cycle <date>: scanned <N> memories + <M> papers + <K> facts, no cross-kind connections surfaced. Self-loop-only patterns: <briefly name them>.", tags=["DREAM:speculative"]).
