# Persist claim_type on hubs (v2)

The extractor returns the claim sort (measurement / definition / capability /
mechanism / landscape); persist it into hub meta so hub_refine can prioritize
thin definitions/landscape claims for corroborators, lint can flag a
capability claim with no regime, and dedup can treat definitions specially.
Design pass first. Owner `src/precis/taproot/`.
