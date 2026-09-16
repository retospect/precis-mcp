# worktree-dapper-toasting-hollerith — continuation prompt

Session of 2026-09-15/16. Everything below is SHIPPED to `main` and
DEPLOYED to the cluster; nothing is half-landed. This file is the
recovery pointer, not a record of the conversation — verify each claim
against the code or prod before acting on it.

## What shipped

| sha | what |
| --- | --- |
| `d5e1d342` | quote-contiguity indicator + neutral paper-context sentence (nanopub artifact + LaTeX hub footnote) |
| `27e5e591` | test pinning the `>=2` threshold guard on the contiguity triple; filed two findings |
| `00c455b2` | `NO_CONTEXT` decline path — the fix for the prod defect below |

**Contiguity.** Two grounding passages of one paper are contiguous iff
their chunks are the same or ADJACENT ROWS of the live body ordering
(`ord >= 0 AND retired_at IS NULL`) — positional, never `ord + 1` (ord
has gaps by design). Computed in `mint.approve()` against the passages
the reviewer actually submitted, frozen into `nanopub_publish.grounding`
so a later re-chunk can't change what a signed artifact says. Emitted as
`precis:excerptsContiguous` on the source DOI node for a >=2-grounding
source only; rendered as "(contiguous excerpt)" / "(non-contiguous
excerpts)" in the LaTeX hub footnote, frozen-preferred with a live
fallback. `passages_contiguous` owns its own `retired_at` filter — do
NOT swap it for `paper_body_chunks`, which lacks one (gr339961).

**Context sentence.** `precis.workers.context_sentence` writes one
neutral method/evidence sentence to `refs.meta['context_sentence']`,
surfaced as `precis:sourceContext` and as a roman "Context:" line
against the italic quotes. Enabled in prod via
`service_config` row `melchior/context_sentence` prio 5 — set
2026-09-15, deliberately ONE host: `_claim` selects unsentenced papers
without locking or stamping, so a `'*'` row would let several hosts
duplicate the same LLM call. Off switch:
`precis service prio melchior context_sentence 0`.

## The defect that flipping the switch exposed — read before extending this

The first prod run wrote *"a review of literature regarding atmospheric
chemistry and planetary science"* onto **Solid C60: a new form of
carbon** (ref 42556). Not a hallucination: that ref has no
`card_abstract`, so the pass fell back to its first body chunk, and ords
0–2 of that ref are a NEIGHBOURING article's ozone bibliography from the
Nature scan. The model described what it was handed.

Load-bearing detail: **none of the five cohort papers has a
`card_abstract`.** The four good sentences came through the same
first-body-chunk fallback. So "require an abstract" is NOT the fix — it
would produce zero sentences and look like success.

The fix (`00c455b2`) makes the TITLE authoritative and gives the model
`NO_CONTEXT` to decline with; a decline is terminal, never retried,
because a retry just invites it to invent one. `NO_CONTEXT` needs its
explicit `is_decline` check because it is short and carries no
blocklisted word — the lint alone would write the token into
`refs.meta` as the sentence.

Prod state after the fix: **4 written, 1 declined** (42556 stamped
`context_sentence_failed`, which also stops regeneration).

## Open

- **Not yet exercised on real data:** no nanopub has been approved since
  the deploy, so the contiguity label and `precis:excerptsContiguous`
  have never met production input. The next multi-quote approval is the
  real check.
- **Tone is unverified by any gate.** The lint enforces the blocklist and
  the word cap, not register. Skim refs 563, 42555, 42557, 42558.
- `docs/backlog/nanopub-gate-chunkid-crashes-instead-of-refusing.md` —
  the mint gate 500s on a non-numeric `chunk_id` instead of refusing it.
  Pre-existing; the backfill SQL's `CASE` guard already defends the
  float case downstream.
- `docs/backlog/mutate-diff-reports-false-survivors.md` — `mutate-diff`
  samples ~5 covering tests per mutant, so it reports false SURVIVED.
  VERIFIED, not inferred: applying the
  `context_sentence.py` `or -> and` mutation by hand makes
  `TestLint::test_empty_is_a_violation` fail, so the killing test existed
  all along. Prioritise this one — a signal that cries wolf is how a real
  survivor eventually gets waved through.
- gr339961 — `paper_body_chunks` missing its `retired_at` filter. This
  work routed around it rather than fixing it.

## Recovery prompt

> Continue the nanopub quote-provenance work in precis-mcp. Shipped and
> deployed: `d5e1d342` (quote-contiguity indicator + neutral
> paper-context sentence), `27e5e591`, `00c455b2` (the `NO_CONTEXT`
> decline path). Read
> `worktree-dapper-toasting-hollerith.README.md` on `main` first, then
> `src/precis/nanopub/__init__.py`'s docstring and
> `src/precis/workers/context_sentence.py`'s module docstring — both
> carry the surviving design rationale.
>
> Next, in priority order: (1) fix `scripts/mutate-diff`'s false-survivor
> reporting per `docs/backlog/mutate-diff-reports-false-survivors.md`;
> (2) approve one multi-quote nanopub so the contiguity label and
> `precis:excerptsContiguous` finally meet real data; (3) the mint-gate
> `chunk_id` crash per its backlog item. Check prod first with
> `scripts/prod-psql "SELECT count(*) FILTER (WHERE meta ?
> 'context_sentence') AS written, count(*) FILTER (WHERE meta ?
> 'context_sentence_failed') AS declined FROM refs WHERE retired_at IS
> NULL;"` — it was 4/1 at handoff.
