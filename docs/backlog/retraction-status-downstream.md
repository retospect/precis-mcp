# Make corrected / expression_of_concern do something downstream

The fetch-time provenance gate populates `retraction_status` for all three
statuses, but the soft flag is display-only. Wire the column into search
ranking + citation grounding with severity-appropriate action: `retracted` =
hard (downrank/exclude, block as a citation anchor); `corrected` /
`expression_of_concern` = soft (annotate, mild downrank). Pairs with the
ROLE3:own citation-grounding filter. Owner `src/precis/runtime/search.py`.
Needs design.
