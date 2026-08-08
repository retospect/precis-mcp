# Trust taxonomy follow-ons (5-state Ⓐ/✍ shipped)

Deferred pieces of the taproot trust ladder.

- Auto-Ⓐ: when full text is unobtainable but the held abstract is present, a
  MEDIUM-tier verify pass ("does this abstract unambiguously support the
  claim?") sets `abstract` machine-earned (`by='verify:abstract'`), gated
  behind the acquiring-arm give-up. Owner `src/precis/taproot/trust.py` +
  `src/precis/workers/chase.py`.
- Calm end-matter exporter section ("Declared-unobtainable sources") listing
  abstract/vouched claims for transparency — they are deliberately excluded
  from the "Unverified claims" problem list. Owner export docx.py/latex.py.
- Perf (if it shows up): batch the frontier-paper meta fetch in
  `claim_trust_bulk` (one `fetch_refs_by_ids` per unverified lifecycle
  finding today).
