# Archived ADRs (superseded — kept for history)

This directory holds Architecture Decision Records that are **fully
superseded** by a later live ADR. They are moved here — not deleted — so the
top-level `docs/decisions/` listing reflects only *currently authoritative*
decisions, while the reasoning behind reversed or absorbed decisions stays one
click away.

The convention that governs what lands here, and how, is
[ADR 0058 — Decision-log archive convention](../0058-decision-log-archive-convention.md).

In short, an ADR is archived only when:

1. a **live successor ADR** already names it as a predecessor;
2. its **filename is unchanged** (so git history follows, and the number is
   never reused);
3. a **one-line archive banner** is prepended (the only edit to the otherwise
   sealed body); and
4. **every referrer** — the index in `../README.md`, the supersession graph,
   and any relative link in a live ADR or `docs/design/*` — is updated in the
   same change.

Full history always lives in git ("Rest in Git"); this directory is a
discoverability aid, not the system of record.

No ADRs have been archived yet — this scaffold lands with ADR 0058. Each chain
condensation is a separate, reviewed change so its referrer updates are
auditable one chain at a time.
