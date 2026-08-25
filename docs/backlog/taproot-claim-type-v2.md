# Persist claim_type on hubs (v2)

The extractor returns the claim sort (measurement / definition / capability /
mechanism / landscape); persist it into hub meta so hub_refine can prioritize
thin definitions/landscape claims for corroborators, lint can flag a
capability claim with no regime, and dedup can treat definitions specially.
Design pass first. Owner `src/precis/taproot/`.

Sizing evidence (td249196): a corpus-search probe over the claim hubs that no
rewriting pass could repair found that 70% were merely missing a link, but a
27% remainder (n=60) are of kinds no instrument can establish — design rules,
recited structural constants, order-of-magnitude estimates, notation
conventions, combinatorial theorems, software-capability statements. Agents
that had never seen the lint sorted them out unprompted, and they concentrate
in sentences carrying no measured quantity (57% linkable vs 83% for sentences
with one). That remainder is what a persisted `claim_type` is for.

Design hazard to answer before building: if a model assigns `claim_type` and
`claim_type` grants a lint exemption, the gate becomes something the model
configures. Keep the exemption table static and per-type
(`_ARTIFACT_LINT_EXEMPTIONS`), or gate the write.

Second field for the same extractor pass: **modality** (experimental /
computational / theoretical, plus whether the source performed the work or
reports another's). Orthogonal to `claim_type` — a DFT elastic-constant
result is measurement-sorted and computational-modality. Sizing and the two
failure modes: `taproot-claim-modality-axis.md`.
