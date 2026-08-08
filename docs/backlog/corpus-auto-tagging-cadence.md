# Corpus auto-tagging cadence (gr51220) — standing process design

"One taxonomy axis per week: pick an axis (`topic:`, `area:`, `status:`…),
sweep under-tagged refs, review, apply" — keeps the corpus navigable without
a big-bang. Two design questions before any build: mechanism (a
level:recurring watch / a job that *proposes* tags for review vs a
manually-kicked sweep) and the review gate (auto-tagging writes to the prod
corpus, so proposals land in a review queue, never apply blind). Deferred
pending Reto picking a mechanism.
