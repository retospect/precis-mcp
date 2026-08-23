---
status: draft
title: "deploy: a host lost mid-bounce-play keeps worker.drain forever — silently stops claiming jobs"
---

# Stranded `worker.drain` after a mid-play unreachable

Hit live 2026-08-23 deploying `53555268`: spark's drain wait ("Wait for
in-flight long jobs leased to this host to finish", delegated to the DB node)
had been polling ~19 min for a live long-job lease when the SSH mux to caspar
broke pipe and the reconnect timed out → ansible marked spark UNREACHABLE and
skipped the rest of the bounce play **including "Remove worker drain file
(resume claims)"**. Result: spark's venvs were already on the new sha and its
workers restarted, but `/opt/precis/worker.drain` stayed behind — the host
silently claims no jobs until someone notices. Nothing alerts on this state;
it looks exactly like "spark is just quiet".

Recovery that time was manual: `rm /opt/precis/worker.drain`, restart the
two daemons the skipped play owed (embedder → wait `/readyz` on :8181 →
watch), verify venv shas.

## Fix directions (either suffices; both is belt-and-braces)

- **Dead-man's switch in the worker** (deploy-independent, preferred): the
  claim loop ignores — or better, deletes-and-logs — a `worker.drain` older
  than ~2 h. A drain is only meaningful for the minutes a bounce takes; any
  old file is a stranded one.
- **Playbook `block:`/`always:`** around touch-drain → wait → bounce →
  remove-drain, so the removal runs even when a task in the block fails.
  Note `always:` does NOT run for an *unreachable* host in the same play —
  so this alone doesn't cover the observed case; it needs a separate
  cleanup play with `ignore_unreachable: true`, or the dead-man's switch.

Related but distinct: `deploy-drain-wait-is-a-silent-noop.md` (historical
job-death re-read after the psqlrc fix). The drain logic itself is correct;
this item is about crash-safety of the drain *lifecycle*.
