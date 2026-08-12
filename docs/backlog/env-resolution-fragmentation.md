---
status: proposed
title: One value, four names, three resolvers — unify env/secret resolution
---
Prompted by the retraction-button timeout of 2026-08-12: melchior's `precis
web` process had no `PRECIS_CROSSREF_MAILTO`, so the draft's Crossref walk ran
outside the polite pool and blew its 90s budget. Nothing was misconfigured in
the sense of a wrong value — the value simply wasn't *in that process*. The
same value was reachable from the worker and the CLI on the same host.

That's the shape of the problem: **the code says what it reads, never what a
process needs; the deploy says what a process gets, never what its code
reads. Neither side can detect the gap, and the failure is a slow degrade,
not an error.** The retraction walk didn't crash — it went anonymous and got
throttled, and the 504 blamed Crossref.

## What's actually fragmented

**One value, four names.** The Crossref polite-pool identity is
`PRECIS_CROSSREF_MAILTO` (`handlers/paper.py`, `handlers/provenance.py`,
`workers/paper_meta_enrich.py`, `cli/provenance.py`,
`precis_web/routes/drafts.py::_crossref_mailto`), `ACATOME_CROSSREF_MAILTO`
(`ingest/lookup.py`, `cli/resolve_metadata.py`), `CROSSREF_MAILTO` (asa's
`deploy/roles/asa_bot/templates/claude_mcp.json.j2`), and on the ansible side
it is sourced from `vault_unpaywall_email`. Setting "the" Crossref contact
means knowing all four. The same sprawl is latent around
`PRECIS_UNPAYWALL_EMAIL` (7 read sites), `EPO_OPS_USER_AGENT`,
`PRECIS_WIKIPEDIA_UA`, `WEB_USER_AGENT` — the polite-pool identity cluster,
which is exactly the class of value that should be one fleet-wide fact.

**Three resolvers with different reach.** `os.environ.get` (per-process,
~265 distinct names read raw under `src/`), `PrecisConfig` (Tier 1, typed,
still env-backed), and `secrets.get_secret` — env → **DB vault** → file →
default. Only the third is host- and process-independent, because the vault
is shared state and env is not. Identity/credential values read through
`os.environ` are therefore reachable from whichever daemons ansible happened
to hand them to.

**The policy guard has a hole exactly where the bug was.**
`docs/conventions/env-vars.md` defines the three tiers and
`tests/test_env_config_policy.py` enforces Tier 1 — but explicitly only under
`src/precis/`; `precis_web` is out of scope by design. The defect was in
`precis_web`.

**Deploy has parity by copy-paste.** Every `precis-*` daemon template loops
`precis_shared_env` (overlay `inventory/group_vars/all/precis_env.yml`) and
then hand-lists daemon-specific keys. Parity across daemons holds only as
long as a var is in the shared block; the moment a code path migrates
process (worker → web route, as retraction checking did) its env does not
follow it, and nothing anywhere notices.

## Proposal — three moves, cheapest first

**1. One name per value, resolved through the vault.** Collapse
`ACATOME_CROSSREF_MAILTO` / `CROSSREF_MAILTO` onto `PRECIS_CROSSREF_MAILTO`,
and switch the identity/credential call sites from `os.environ.get` to
`secrets.get_secret`. A value set once in the vault then reaches every daemon
on every host with no ansible edit and no redeploy — which is the structural
answer to per-process parity, not a bigger shared-env block. Precedent is
in-tree and already blessed for kinds: `KindSpec.requires_secret` exists
precisely "for credentials that live in the secrets vault, so the kind stays
available after the env var is pulled and the value lives only in the DB"
(`precis/protocol.py`). Scope this to the identity cluster first; leave
topology/toggle vars (`PRECIS_NODE`, `PRECIS_*_ENABLED`) on env, where
per-process really is the point.

**2. Declare what a process needs; say so at boot.** Generalize
`requires_secret` from kinds to entrypoints: each daemon (`precis web`,
`precis worker --profile …`) declares the values its enabled code paths
depend on, and logs one line at startup naming anything unresolved. Not a
hard fail — running without a polite-pool contact is a legitimate degrade —
but it converts a silent degrade into a visible one, and gives `/status` and
the deploy preflight something to assert. This is the piece that would have
caught the actual bug.

**3. Widen the Tier-1 guard to `precis_web`.** Same test, wider glob. The
service that serves the UI is not less deserving of the policy than the one
that serves MCP.

## Immediate, separate from the above

`PRECIS_CROSSREF_MAILTO` should be added to `precis_shared_env` in the
overlay (`inventory/group_vars/all/precis_env.yml`, sourced from
`vault_unpaywall_email` as the asa template already does), so every daemon
carries it. That's a one-line overlay change and does not wait on any of the
three moves — see the `melchior_overlay_sync` runbook for the push path.
