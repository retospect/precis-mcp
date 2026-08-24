---
status: ready
title: qu164903 dossier audit residuals — export cite parity, prod correction
prio: high
---

# qu164903 dossier audit residuals

<!-- Origin: 2026-08-24 dossier audit (the "corner saga"). Root fix shipped
14e78677 (periodic-symmetry canonicalization + tick-prompt tiling rules).
Slice A (web sourcing/render: provenance_state computational-evidence class,
LINKIFY_KINDS realign, structure sigil, cited-sources rail) and slice B
(barrier-trust guards: absurd-magnitude auto-untrust, unrelaxed-geometry
flag, symmetry-twin disagreement untrust) shipped in the follow-on commit —
only the items below remain. Findings verified against prod structures
(translation twins st245406≡st237458, st243092≡st239974, energy-identical). -->

## A-residual. Export cite parity (lower prio)

`src/precis/export/latex.py::_render_target` and `docx.py` silently DELETE
non-paper handles on export; `_collect_raw_cites`
(`handlers/_citations_view.py`) and `smartdraft.py::cite_integrity_ok` skip
them too. Teach them the same computational-evidence class the web render
now has (`_COMPUTED_EVIDENCE_KINDS` in `routes/drafts.py` /
`routes/smartdraft.py` — consider hoisting to one shared home when a third
consumer appears).

## C. Prod campaign correction (Reto-run, per prod-mutation-needs-user-permission)

Exact commands prepared 2026-08-24 (run in this order; step 0 is agent-runnable):

**0. Deploy first** — canonicalization (14e78677) + the slice-B guards must be
on the cluster before the next tick: `/go` from a synced tree.

**1. Untrust the two audit-condemned barriers** (no CLI verb for meta stamps —
handler-direct script per `docs/runbooks/prod-one-off-cli.md`). Write locally
via the Write tool, then two plain commands (no heredocs — Bash guard):

```python
# /tmp/qu164903-untrust-claude.py
from precis.runtime import build_runtime

rt = build_runtime()
store = rt.store
assert store is not None
store.stamp_ref_meta(243092, {
    "barrier_trusted": False,
    "barrier_twin_disagreement": "st239974",
    "barrier_untrust_reason": (
        "audit 2026-08-24: exact translation twin of st239974 (same crystal "
        "after (1/3,1/3) shift, identical relaxed energy -188.457 eV); "
        "barriers 4.9926 vs 0.479 eV on one geometry = irreproducibility, "
        "not chemistry"),
})
store.stamp_ref_meta(211611, {
    "barrier_trusted": False,
    "barrier_untrust_reason": (
        "audit 2026-08-24: relax converged in 0 steps (never relaxed); H at "
        "atop site, not the hollow the campaign believes; 0.355 eV unverified"),
})
print("untrusted st243092, st211611")
store.close()
```

```
cat /tmp/qu164903-untrust-claude.py | ssh -o IdentityAgent=none melchior 'cat > /tmp/qu164903-untrust-claude.py'
ssh -o IdentityAgent=none melchior 'export PRECIS_DATABASE_URL="$(/usr/libexec/PlistBuddy -c "Print :EnvironmentVariables:PRECIS_DATABASE_URL" /Library/LaunchDaemons/com.precis.web.plist)"; /opt/precis/venv/bin/python /tmp/qu164903-untrust-claude.py'
```

Success criterion: prints `untrusted st243092, st211611`; frontier then shows
both as excluded (`barrier_trusted false`).

**2. Ledger note** (after the stamps, so "now untrusted" is true; `precis
tools` reads the env DSN, no `--database-url` flag):

```
ssh -o IdentityAgent=none melchior 'export PRECIS_DATABASE_URL="$(/usr/libexec/PlistBuddy -c "Print :EnvironmentVariables:PRECIS_DATABASE_URL" /Library/LaunchDaemons/com.precis.web.plist)"; /opt/precis/venv/bin/precis tools put --kind quest --id 164903 --entry decision --text "Audit (2026-08-24, human-verified): under periodic boundary conditions the corner and central placements are the SAME crystal — st245406≡st237458 and st243092≡st239974 are exact translation twins (identical relaxed energies; coordinates match after a (1/3,1/3) lattice shift). The corner-placement narrative and the 2x2 corner grid are null experiments — drop them. The H-decoupling collapse (4.99 eV on st243092 vs 0.479 eV on st239974) is barrier-pipeline irreproducibility on one geometry narrated as chemistry; both barriers are untrusted — re-measure before ranking. Champion st211611: relax converged in 0 steps (never actually relaxed) and its H sits at an atop site, not a hollow — its 0.355 eV barrier is untrusted; re-relax and re-measure before treating it as champion. st245914 was mis-built (H atop Sn, not the champion offset) — rebuild if that comparison matters. The Cd 0 eV reading (dc3176806) needs a re-check. Placement language going forward: site type + offsets relative to co-adsorbates; absolute cell positions do not exist in a periodic cell."'
```

Success criterion: exit 0 + "logged decision on quest id=164903"; next tick's
dossier drops the corner narrative (~12 infected chunks: dc3140949/51,
dc3162168, dc3173095/96, dc3176801–04, dc3176808; also dc3023253, dc3136837).

**3. Re-measurement rides the loop** — no manual re-dispatch: with the
barriers untrusted and the ledger note in place, the quest's own
promotion/dispatch machinery re-measures (a manual `dispatch_relax` of
st211611's unchanged geometry would content-address onto the old 0-step
result anyway; the corrected hollow-site geometry should be re-proposed by
the tick, post-canonicalization).

## Acceptance

- A-residual: a smartdraft citing `[stNNN]` exports to latex/docx with the
  handle rendered (not silently dropped), and `cite_integrity_ok` counts it.
- C: next qu164903 tick's dossier drops the corner narrative and cites the
  ledger note; dc3176803 renders sourced on the dossier (shipped web fix,
  verifiable after deploy).
