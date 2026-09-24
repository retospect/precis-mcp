---
status: draft
title: ingest Claude Code session history into precis as conv refs
prio: normal
---

# ingest Claude Code session history into precis

Reto, 2026-09-24: "we should ... log all session history _into_ precis somehow."

## Motivation / why

Session history is currently write-only. Every worktree session produces a
detailed record of how something was actually built, diagnosed, and got
wrong — and that record is reachable only by a human scrolling a terminal,
until compaction summarises it away. What survives is whatever the session
thought to hand-write into auto-memory, which is a lossy, hand-curated
digest of a much richer artifact.

The cost is re-derivation. Sessions re-investigate things a sibling already
settled, re-hit gotchas that a previous transcript explains in full, and
re-learn the shape of a subsystem from scratch. Auto-memory mitigates this
but only for what someone remembered to write down, in the compressed form
they chose at the time.

A concrete case from the session that filed this item: a gate appeared hung,
and it was killed at 95% after ~50 minutes of work. The relevant knowledge
already existed in auto-memory ("`docker inspect` can hang forever, **looking
like hung pytest**") and was still read backwards under time pressure. The
transcript of the session that originally learned that lesson — with the
actual process table, the actual symptom, the actual reasoning — would have
been decisive in a way a one-line memory hook was not. That transcript
exists on disk and nothing can search it.

Precis is a system for making a corpus searchable and citable. Its own
construction history is a corpus it does not hold.

## In scope

**Substrate: the existing `conv` kind.** `ConversationHandler` is already
"durable chat-thread refs — each `conv` ref is a captured conversation; turns
live as blocks (one block per message in chronological order)", with a
`/transcript` view and search across turns. That is the shape a session is.
No new kind.

**Source.** Claude Code writes one JSONL per session under
`~/.claude/projects/<path-mangled-dir>/<session-uuid>.jsonl`. The directory
name encodes the project path, so a session is already attributable to a
repo and (for worktree sessions) to a worktree.

**An ingest pass** that turns a finished session into a `conv` ref: title
from the session's subject, turns as blocks, metadata carrying the session
id, the worktree, the branch, and the shas the session shipped. Idempotent
on the session uuid so a re-run updates rather than duplicates.

**Selective turn capture.** A raw transcript is mostly tool traffic. The
default should keep user turns, assistant prose, and tool *calls* with their
arguments — and summarise or drop bulk tool *results* (file reads, test
output, log dumps). Those are the bytes, they are the least reusable
content, and they are what would swamp the embedding budget.

**Redaction before write.** Session transcripts routinely contain the things
the repo's own secret gate exists to keep out: tailnet and LAN addresses,
prod DSNs, vault material, pasted credentials. The repo is public and the
precis DB is not, but "not public" is not the same as "safe to embed and
surface to every agent" — a credential in an embedded chunk is a credential
in search results. This needs its own pass, reusing the deploy-tree secret
patterns where they apply.

## Explicitly NOT in scope

- **Replacing auto-memory.** Memory is the curated, always-loaded index;
  ingested sessions are the searchable long tail behind it. Both.
- **Real-time streaming.** Ingest finished sessions, on a schedule or on
  session end. A live tail is a much harder problem for much less value.
- **Ingesting every historical session on day one.** Start with a window
  (recent sessions) and widen once the volume and redaction behaviour are
  measured.
- **Cross-machine collection.** This machine's sessions first.
- **Embedding raw tool output.** See selective capture above.

## Acceptance criteria

- A finished session appears as a `conv` ref whose `/transcript` reads as
  the conversation, with tool noise collapsed rather than verbatim.
- Re-running ingest on the same session updates its ref; it does not mint a
  second one.
- `search(kind='conv', q=...)` finds a session by something discussed in it,
  and the hit names the worktree and branch.
- A session containing a known-secret pattern (a prod DSN, a tailnet
  address) has it redacted in the stored blocks — verified by a test with a
  synthetic transcript, not by inspection.
- Ingesting a large real session does not blow the chunk budget: measure
  chunks-per-session and state the number in the item before `--apply`.

## Target + blast radius

New ingest module under `src/precis/ingest/`, a CLI entry point, and the
existing `conv` handler as the write target. Touches the chunk/embedding
cascade by volume — the main risk here is corpus dilution, not correctness:
thousands of conversational chunks competing with papers in search results.
Whether `conv` chunks should be weighted down, or excluded from cross-kind
search by default, is a decision this item must make before it ships.

## Open questions / decisions log

- **Dilution.** Should session chunks participate in ordinary cross-kind
  search, or only in `kind='conv'`-scoped search? Leaning scoped-only at
  first — a dev-session chunk outranking a paper on a science query would be
  a bad trade, and scoping is reversible.
- **What counts as a turn?** One block per message is the `conv` contract,
  but a single assistant message can carry a dozen tool calls. One block per
  message with calls summarised inline, or one block per call?
- **Retention.** Do sessions expire? `refs.auto_refresh_days` exists for
  decaying relevance and may be the right default rather than permanence.
- **Attribution.** `set_by` is a channel vocabulary (`agent`/`user`/…), so
  which human drove a session needs somewhere else to live — same question
  the paper-review-notes item answered with `web_users.abbrev`.
- **Does this subsume `agentlog`?** `agentlog` records cluster agent *runs*;
  this records interactive sessions. They look adjacent. Decide whether
  they converge or stay distinct before building a second overlapping
  surface.
- **Privacy posture.** Sessions contain unguarded thinking, half-formed
  judgements about people's work, and pasted third-party content. Worth an
  explicit decision on what is acceptable to make searchable, rather than
  discovering it after the fact.
