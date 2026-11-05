# Controlled U2 access order experiment

The accepted November 5 placement satisfies all 15 hard distances. Its one
fresh route connects ground but leaves five signal nets. Astra's finite
shared-predicate audit (`me567551`, task scratch
`astra-terminal-predicate-review-1105.md`) finds baseline legal leg/via
witnesses for U2.VBAT_OV and U2.VRDIV that saved VBAT_UV track 53 blocks.
The saved final copper is not an exact intermediate search occupancy, so
this is a hypothesis for a controlled experiment, not a cause certificate.

Run at most one additional full route with unchanged candidate, catalog,
pad mapping, poses, layers, physical rules, fabrication process and effort.
Use the reviewed silk fix only for output. Do not move resistors or other
parts in this experiment. No private capture, replay or search-state dump.

Use a task-local wrapper around the existing `_net_order` seam; never edit
the engine or its defaults. Saved spans predict the relative native order
VBAT_OV (54.223709 mm), VBAT_UV (74.755480 mm), VRDIV (85.844440 mm).
Confirm that ordering in the first actual full callback; refuse if it
differs or the three identities cannot be resolved. Promote only VRDIV
to immediately before VBAT_UV. Keep VBAT_OV at its native position and
preserve every other net's relative order. When the three are not all
active multi-terminal nets, retain the native order; if a later callback
already puts VRDIV before VBAT_UV, leave it unchanged. Record every native
and controlled order and prove the intended permutation. All routing,
retry, shove, clearance and final acceptance logic stays real.

This tests earlier VRDIV access and its effect on survivor topology. It
does not test whether VBAT_UV initially preceded both failed victims:
VBAT_OV already precedes it. Saved final-copper obstruction alone does
not identify the cause of that earlier VBAT_OV failure.

Before running, preserve the original run and freeze source, input and
wrapper hashes. Independently validate all poses, 159 physical pads, 15
distance bounds, courtyard separation and plane policy. Confirm every
routing module matches the original producer; report silk-only source
differences explicitly. Preserve standard route/model/copper/DRC/events
outputs even if later export fails. Correct the previous label-census
dataclass postprocessor bug in this task's wrapper, not by rerouting.

Compare signal completion, the five original failed nets, all previously
successful nets, ground components/fanouts, committed-copper DRC, route
time, tracks/vias and output readback. An improvement needs preserved
ground and physical legality, not only success at the two victims. A
failure or regression is a useful bounded result; do not start an order
matrix, effort increase or second attempt. Export/read back the result
without another route. Publish only a clearly labelled partial preview
selected using the complete comparison, under the existing data-upload
authorization; application deployment and manufacturing approval remain
outside this experiment.
