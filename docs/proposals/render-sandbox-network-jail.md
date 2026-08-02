---
status: draft
title: Render-sandbox Phase 2 — network + filesystem jail
model: opus
---

# Render-sandbox Phase 2 — network + filesystem jail

> Improvement-plan P2 item (2026-08-02 review). Phase 1
> (`src/precis/render/sandbox.py`: stripped env, rlimits incl. the
> `RLIMIT_NPROC` fork-bomb clamp, wall-clock kill, throwaway CWD,
> `python -I`) is complete. This proposal designs the remaining jail:
> **no network, no filesystem beyond the throwaway workdir**. The
> `render_python()` contract and its call sites (`render/figure.py`)
> do not change — Phase 2 swaps the launch mechanism under the same
> seam, exactly as the module docstring promised.

## Threat model (what Phase 2 actually closes)

Render code is LLM-authored Python inside a figure chunk; an LLM that
ingests external content can be steered into writing hostile code
(indirect prompt injection — ADR 0035 §3). Phase 1 already guarantees
the child holds **no credentials** and can't exhaust the host. What it
does *not* stop:

1. **Network egress.** The child inherits the host's network. It can
   reach every tailnet service directly — bypassing `safe_fetch`
   entirely. This is sharpened by a standing accepted risk: the precis
   web UI is deliberately **unauthenticated** (tailnet-only,
   single-user — see `docs/improvement-plan.md` "Accepted risks"),
   including `/console`, a generic verb runner. A hostile render
   reaching `precis_web` can therefore *write to prod* without any
   credential. The jail is the compensating control that keeps that
   accepted risk acceptable.
2. **Filesystem reads.** The child runs as the worker's UID and can
   read anything world/user-readable — the corpus on the NAS mount,
   `~/.ssh` (worker UID's own home; HOME is redirected but paths are
   guessable), cluster config. Combined with (1), that's read + exfil;
   with the network cut, the only exfil channel left is the PNG/stderr
   returned to the caller — low-bandwidth, visible, and attributable.
3. **Memory on macOS.** macOS ignores `RLIMIT_AS`, so the Phase-1
   memory cap is Linux-only today. A container's cgroup limit fixes
   this on any host with a runtime.

Non-goals: defending against a malicious *operator*; sandboxing
anything other than the `render_python` seam (jobs-lane code has its
own ADR-0048 `sandbox_run` design); kernel-exploit-grade isolation.

## Decision (proposed): a jail ladder behind the same seam

`render_python()` picks the strongest available mechanism at call time,
in order. One env knob, `PRECIS_RENDER_JAIL` = `auto` (default) |
`container` | `seatbelt` | `none`, pins or disables the choice;
`RenderResult` gains a `jail: str` field naming what actually ran (so
the figure failure-bubble and tests can assert on it).

### Rung 1 — container (`--network=none`), Linux + any host with a runtime

Run the existing harness in a dedicated minimal image:

    podman run --rm --network=none --read-only \
      --tmpfs /work:rw,size=256m --workdir /work \
      -v <throwaway-dir>:/work:rw \
      --user 65534:65534 --pids-limit 256 --memory 1g \
      --security-opt no-new-privileges \
      precis-render:latest python -I /work/harness.py /work/spec.json

* Image `precis-render`: python + matplotlib/numpy (+ pandas if audit
  of existing figure chunks shows use) — built in `infrastructure/`
  next to the dev image, tag pinned by deploy, **no precis code baked
  in** (the harness is bind-mounted per render, so image rebuilds are
  rare).
* Closes network, filesystem, *and* the macOS memory gap in one move.
* Cost: ~300–500 ms container start per render — negligible against
  the 30 s render ceiling.
* Availability: spark (Linux) natively; Macs only where a podman
  machine/Docker Desktop is running. The ADR-0048 `sandbox_run` slice
  already stubs podman detection — reuse its "is a runtime usable"
  probe rather than writing a second one.

### Rung 2 — macOS `sandbox-exec` (seatbelt) profile

No container runtime on the Mac workers (launchd-native daemons) is the
common case, and macOS has no network namespaces. `sandbox-exec` is
deprecated-in-name but load-bearing across the OS (and used by Bazel,
Chromium); it can express exactly what we need:

    (version 1)
    (deny default)
    (allow process-exec process-fork)
    (allow file-read* (subpath "/usr") (subpath "/System")
                      (subpath "<python-prefix>") (subpath "<workdir>"))
    (allow file-write* (subpath "<workdir>"))
    (deny network*)

Profile is generated per render with the concrete workdir/python paths
substituted; child launched as
`sandbox-exec -p <profile> python -I harness.py spec.json`, keeping the
Phase-1 rlimits/preexec unchanged underneath. Blocks network and
filesystem writes outside the workdir, and narrows reads to the
interpreter tree + workdir. Doesn't fix the macOS memory cap (lives
with it — Phase 1 status quo).

### Rung 3 — Linux userns fallback: `unshare -Un`

Linux host without a container runtime: `unshare --user --net` gives an
empty network namespace (loopback only, no interfaces) with no root
needed. Network cut; filesystem stays Phase-1. Cheap insurance rung —
implement only if a creds-less Linux worker without podman actually
exists in the fleet (today spark has podman; if that holds, skip this
rung and let Linux-no-runtime fall to rung 4 with a warning).

### Rung 4 — Phase-1 floor

What runs today. Selected only when nothing stronger is available;
logged at WARNING once per process (not per render) so a fleet node
silently degrading to no-jail is visible in the worker log and can be
alerted on by the nursery.

## Failure semantics

A jail-mechanism failure (podman daemon down, seatbelt profile rejected)
must degrade the *same way a render bug does*: `RenderResult(ok=False,
error="jail:<detail>")` — never fall back silently to a weaker rung
mid-render. Rung selection happens once per process and is sticky;
degradation is a startup decision, not a per-render race.

## Testing

* Unit: rung selection (env knob × fake availability probes); profile
  generation (paths substituted, no workdir escape in the template).
* Integration, gated on runtime availability (skip-marked like the
  other podman-gated tests): inside rung 1 and rung 2, a render that
  attempts `socket.connect()` to a local listener must fail; a read
  outside the workdir must fail (rungs 1–2); a legitimate matplotlib
  render must still produce a PNG under every available rung.
* The existing Phase-1 tests (`tests/test_render_sandbox.py`) keep
  passing untouched under rung 4.

## Open questions

1. **Image contents:** audit live figure chunks for what they actually
   import before pinning the `precis-render` image manifest (matplotlib
   is certain; numpy near-certain; pandas/scipy TBD).
2. **Mac runtime posture:** is running a podman machine on the Mac
   workers acceptable operationally (RAM cost on already
   jetsam-sensitive melchior), or is seatbelt the designated Mac
   endgame? Proposal assumes seatbelt is the Mac default and containers
   are opportunistic.
3. **Nursery alert:** should "node degraded to rung 4" become a
   `critical` nursery alert or stay a log line? Leaning log-only —
   it's a posture regression, not an outage.
