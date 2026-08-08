# pathway meta.measures has no writer

The reaction-pathway explorer renders `refs.meta.measures` per state, but
PathwayHandler has no `edit()` and `put()` drops meta kwargs — no MCP verb
can define a measure (only test seeds / raw SQL). Add
`edit(kind='pathway', id, measures=[{name, op, atoms, element?}])` validating
op ∈ {distance, angle, min_distance}, writing meta.measures. Until then the
explorer's measures panel is dark on prod pathways. Sibling motion work:
docs/proposals/pathway-frame-capture.md. Owner
`src/precis_pathway/handler.py`. Sonnet-shaped.

test: edit-verb round-trip → measure card renders on /refs/pathway/{id}.
