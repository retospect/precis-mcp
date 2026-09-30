-- 0171_pcb_net_electrical_spec.sql
--
-- docs/backlog/pcb-missing-constraint-classes.md §E-1 ("Voltage separation
-- as a routing rule — and it is PAIRWISE"), plus the annotation half that
-- section's survey (2026-09-29) found already built and wired to nothing.
--
-- `pcb_nets` has carried ONE electrical annotation since 0047 —
-- `est_current_a`, which `precis.pcb.rules.resolve_net_rules` turns into an
-- IPC-2221 track width and a via-array count. The two other numbers a
-- datasheet actually yields have had nowhere to land:
--
--   * **working voltage**. `precis.pcb.capabilities.conductor_spacing_mm`
--     already implements IPC-2221B Table 6-1 exactly (B1/B2/B4 columns,
--     internal/external, coated/uncoated, refusing above the 500V top band
--     rather than extrapolating) — and its only caller is the EWOD
--     generator's `hv_separation`. Required spacing is a function of
--     |V_a - V_b|, a property of the PAIR, which is why it cannot ride the
--     per-net resolver: `drc.check_clearance` already takes the stricter of
--     two nets' resolved clearances, and that `max()` is the hook.
--   * **edge rate / impedance**. `precis.pcb.objectives.NetAnnotation` is
--     documented as "LLM-derived at part ingestion from datasheet timing
--     tables and input specs", and `annotation_for(function_hint, override)`
--     exists to consume it — but `cost.py::_annotation` always calls
--     `annotation_for(None)` and `CostConfig.net_annotations` was populated
--     only by a test. `function_hint` had no producer anywhere in src/.
--
-- All four columns are NULLABLE and mean "not annotated", never zero: a
-- board that annotates nothing must behave exactly as it did before this
-- migration (the same discipline `est_current_a`'s own NULL path follows,
-- and the same "undefined != zero" rule `cost.py` enforces). No CHECK on
-- `function_hint` — the vocabulary is
-- `precis.pcb.objectives._FUNCTION_DEFAULTS`, a Python-side fallback
-- library that is expected to grow, and an unknown hint degrades to the
-- conservative unknown default rather than failing a write.
--
-- Explicitly NOT here: length-match groups and differential-pair partners.
-- Neither exists in the engine (`diffpair` is only a routing-objective
-- preset NAME), so a column for either would be storage with no consumer.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot
-- after merge (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

ALTER TABLE pcb_nets
    ADD COLUMN IF NOT EXISTS working_voltage_v  double precision,
    ADD COLUMN IF NOT EXISTS edge_rate_v_per_ns double precision,
    ADD COLUMN IF NOT EXISTS impedance_ohm      double precision,
    ADD COLUMN IF NOT EXISTS function_hint      text;

COMMENT ON COLUMN pcb_nets.working_voltage_v IS
    'Peak working voltage of this net, volts (§E-1). NULL = not annotated. '
    'Consumed PAIRWISE: required clearance between two nets includes '
    'capabilities.conductor_spacing_mm(|V_a - V_b|, ...) — a scalar per-net '
    'attribute cannot express the constraint, so this column is an input to '
    'a pair computation, never a per-net clearance.';

COMMENT ON COLUMN pcb_nets.edge_rate_v_per_ns IS
    'Datasheet-derived switching edge rate, V/ns — the aggressor half of '
    'objectives.NetAnnotation. NULL = quiescent/DC, not asserted an '
    'aggressor without evidence.';

COMMENT ON COLUMN pcb_nets.impedance_ohm IS
    'Datasheet-derived driving-point impedance, ohms — the victim half of '
    'objectives.NetAnnotation. NULL = unknown, treated as high-Z '
    '(worst-case victim), never as zero.';

COMMENT ON COLUMN pcb_nets.function_hint IS
    'A precis.pcb.objectives._FUNCTION_DEFAULTS key (crystal, switcher_sw, '
    'adc_input, digital_logic, power_rail) naming what this net DOES, used '
    'to pick a fallback NetAnnotation when the explicit columns above are '
    'NULL. Unvalidated on purpose: an unknown hint degrades to the '
    'conservative unknown default rather than failing the write.';

COMMIT;
