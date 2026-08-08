# Draft footnotes + annotations (deferred design)

Footnotes: a first-class `footnote` chunk_kind anchored to its block via
meta.anchor, out-of-flow, embedded + citable, ships in export — parallels
term/figure/caption. Annotations: a separate editorial layer NOT in
reading_order; a `draft_annotation` chunk_kind + meta.anchor + meta.author,
append-only via chunk_events (the gripe_comment idiom), never exports.
