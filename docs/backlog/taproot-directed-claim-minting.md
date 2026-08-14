---
status: idea
title: Taproot directed claim minting — follow-ons (MCP verb, qualify eval)
model: opus
---

# Directed claim minting — follow-ons

v1 (the "argue-out" mint: `qualify_claim` against a passage +
`directed_mint`'s cascade placement, dry-run-default CLI) shipped
2026-08-14. Present-state truth:
`src/precis/taproot/directed.py` module docstring. Two pieces of the
original design are still open:

- **MCP-verb surface.** Today's only front doors are the CLI
  (`precis taproot direct-mint`) and the Python API
  (`precis.taproot.directed.qualify_claim` / `directed_mint`) — there is no
  `directed_mint` MCP tool call. Needed once a natural consumer (quest
  gap-filling, draft "can I say this?" authoring, the nanopub
  negative-results pathway) wants to call it in-process rather than via
  CLI/API.
- **Eval on the extraction-fixture pattern.** `qualify_claim` runs BIG tier
  specifically because the judgment call (spotting what a passage does
  NOT license, finding the strongest honest weakening) is exactly where
  small models over-agree — but that tier choice is un-evaluated. No
  fixture set for the qualify step exists yet alongside
  `tests/fixtures/taproot/` (`claim_pairs.jsonl`, `extraction_passages.jsonl`,
  `labels_fable.json`, `labels_opus.json`, which cover `extract_claim`, not
  `qualify_claim`).
