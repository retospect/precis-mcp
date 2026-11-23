# Anchors

An anchor records an observed location, its verbatim quote and a fingerprint in
`anchors`. Relocation writes the current location and state to
`anchor_resolutions`. The observation stays immutable, and its handle uses
the same sequence for all three target types.

| Handle | Position | Fingerprint |
|---|---|---|
| `at<N>` | Ref, chunk, zero-based half-open character range | Chunk content SHA |
| `ac<N>` | Repo, file and symbol path, one-based inclusive line range | Commit SHA |
| `ap<N>` | Ref, structured kind and selector | Ref content SHA |

The resolver tries the observed position when the fingerprint matches. After
a mismatch it searches for the exact quote in the ref or repository. A unique
match can move the anchor. A missing or ambiguous quote gives a stale
resolution. Embeddings may suggest candidates for review.

## Consumers

- Measures mint text anchors for resolvable primary and extra spans in the
  measure write transaction. `measure_anchors` associates the new IDs with
  the measure without changing its frozen fields or review SHA.
- Finding evidence links with a source chunk use `evidence_anchors`. They
  anchor the exact quote when it occurs once in the chunk and otherwise
  anchor the full passage. Ref-level evidence can still be recorded.
- Python source citations return the shared `ac<N>` handle alongside the
  compatible `an<N>` alias. The migration links eligible source observations
  to the store. Location-only aliases remain readable with their legacy status.
- The structured store accepts a kind adapter's selector and exact observation.
  The adapter supplies current selectors and observations for relocation.
  Domain-specific selectors and OCR correction records need their own producers.

## Existing measures

The migration links primary `offsets` and compatible `raw_offsets` spans
whose live chunk and range can be verified. Run
`scripts/backfill-measure-anchors` to report the remaining legacy spans.
Use `--apply` to link resolvable spans with the current measure parser, including
sentence, sentence range and numeric atom selectors. Rows without a verifiable
location retain their recorded data. Neither pass rewrites a review.

The text mint door accepts an explicit verified range or a quote unique in its
chunk. Existing paper sentence views and `s<N>` measure writes remain compatible.
The measure writer converts these selectors to ranges when it can resolve them.
The original selector remains on the measure for review compatibility.

Handle resolution: `ac<N>` resolves through `get(kind='python', id='ac<N>')`.
`at<N>` and `ap<N>` are store-level handles for now (`Store.get_anchor_by_handle`);
no read door resolves them yet, so measure detail does not print them.
