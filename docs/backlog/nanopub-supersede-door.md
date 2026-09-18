# nanopub supersede door for our own anchored hubs

`precis-nanopub-help` and `nanopub/demote.py` (`ACTION_SUPERSEDE_REQUIRED`)
say an anchored/published hub whose evidence or wording must change is
"a human supersede", but no write door exists: `cli/nanopub.py` has
`reopen` (pre-anchor only) and `re-stamp` (batch-scoped), and
`superseded_by` is only *derived* for other people's nanopubs in the
mirror (`store/_nanopub_mirror_ops.py`). `TERMINAL_STATES` lists
`superseded` with nothing that writes it. First real case: fi191121
(gr345628) anchored 2026-09-18 before its grounding fix (a third
definition quote, pc35887) landed — the artifact cannot take the new
passage and cannot be marked superseded by a re-mint. Owner
`src/precis/nanopub/` (mint + a `supersede FI` CLI verb that mints the
successor from the hub's current evidence, links `supersedes` old→new,
flips the old row terminal, and refuses on a published row without
`--live` acknowledgement). test: an anchored, unpublished row +
successor mint → old row `superseded`, new row `candidate`, edge present.
