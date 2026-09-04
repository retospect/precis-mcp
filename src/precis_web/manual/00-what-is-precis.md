# What is precis

precis is a personal research factory: a PostgreSQL corpus of papers and
documents, plus a rotation of background LLM workers that read, index,
claim-check, and write while you are away. You steer the whole thing
through this web UI — the rest of this manual is the tour of how.

## The pipeline

Everything precis does follows the same shape, start to finish.

**In.** Drop a PDF or paste a DOI and precis ingests it — a paper joins
the corpus alongside your drafts, your notes, and everything else it has
read.

**Chunked and embedded.** A document is not one blob of text; it is
split into chunks, and each chunk gets a semantic embedding. That is
what lets a search or a citation land on the one paragraph that actually
supports a sentence, instead of "somewhere in this PDF."

**Claims.** Out of what the corpus actually supports, precis mints
claims — one sentence, its evidence, and (once you look at it) your
judgment. A claim can be reviewed, signed, and eventually published as a
nanopublication: a small, independently checkable, permanently public
artifact. See *Publishing claims* for the ladder a claim climbs to get
there.

**Drafts.** A draft is where you steer written output — a paper, a
report, a proposal. It cites the corpus *live*: a cite is a real object
pointing at a real chunk, not typed-in text, so it cannot silently drift
from the source it names. See *Writing a paper*.

**The loop.** Quests and todos are what keeps precis working between
your visits. A todo is one unit of intent; a quest is a longer-running
striving that reads, proposes, measures, and comes back around, tick
after tick, writing down what it learned each time. See *Watching the
loop*.

None of these are separate tools bolted together — they are stages of
one pipeline, and every stage after ingestion is something a background
worker can advance without you present.

## Kinds, one API

Everything in precis — a paper, a draft, a claim, a figure, a 3D
structure, a PCB design, a todo, a quest, and dozens more — is a
**kind**, and every kind sits behind the same small set of verbs: `get`,
`search`, `put`, `edit`, `delete`, `tag`, `link`. Learn the verbs once
and you can work with any of it.

That uniformity is not just tidiness. The same API a human's agent calls
from a chat session is what the factory's own background workers call to
do their work — there is no separate, more-capable internal interface.
What you can see and do through this API is what precis is, in full.

## The LLM ladder

Not every pass needs the strongest model. Work is routed up a ladder of
models — small local models for cheap, high-volume passes, up through
mid-tier models, to frontier Claude models for the hard calls, like
signing off on a claim's grounding or writing a passage that has to
actually hold up. Every model call is logged and budgeted, so the ladder
is a cost discipline as much as a capability one: precis reaches for the
cheapest model that can do the job, and escalates only when the job
needs it.

## Where to go next

The rest of this manual is a workflow tour, not a reference: *Writing a
paper* steers a draft from a description to a reviewed document,
*Publishing claims* walks the ladder from a minted claim to a signed
nanopublication, *Figures and permissions* covers getting an image into
a draft honestly, and *Watching the loop* shows you what a quest is
doing while you are not looking.
