---
id: patent-claim
title: precis — patent claim (section style)
summary: write one patent claim per chunk — preamble + comprising + elements, antecedent basis; dependent claims reference others by [dc…]
status: active
style: patent-claim
role: section
archetype: managed
manages: [claim]
---
You are writing **one patent claim** — exactly one independent or dependent claim per chunk, as a single grammatical sentence. Structure: a preamble naming the category (e.g. "A method for…", "An apparatus comprising…"), the open transitional word "comprising", then the elements, each introduced with "a"/"an" on first appearance and referred back to with "the"/"said" thereafter — keep antecedent basis intact.

For an **independent** claim, recite the complete combination standalone. For a **dependent** claim, open by referencing its antecedent claim by handle and noun phrase — "The method of [dc…], further comprising…" — and add only the narrowing limitation. Indent elements as a list within the single sentence; end the whole claim with one period.

Use formal, impersonal, present-tense language; no marketing. The claim feeds the `claims` series — write it as positioned, and refer to other claims by `[dc…]`; the claim number is rendered from order at export (managed numbering is an expansion, ADR 0037 §5). Keep antecedent basis as you write; residual breaks are caught by a patent review pass (ADR 0037 §3a). Cite corpus material, if ever, as `[pc…]`.
