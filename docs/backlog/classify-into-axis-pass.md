# Retire classify.py + classify_topics.py onto axis_pass

`workers/axis_pass.py` already parameterizes the claim → LLM-classify → tag-write shape over any axis definition.

## Current state

Two hardcoded workers coexist:
- `workers/classify.py` (464 LOC) — claim classification
- `workers/classify_topics.py` (394 LOC) — topic axis (similar shape)

Both duplicate:
- `_extract_json`
- `_load_axis`
- `_classify_one`
- `_build_prompt` / `_build_chunk_prompt`
- Verbatim `_render_examples` (axis_pass.py's docstring notes this copy was deliberate "per the additive-only brief")

Canonical: `workers/axis_pass.py` (~700 LOC true duplication).

## Migration path

1. Convert axis definitions to YAML under `data/axes/` (parametrize the axis spec, prompt instructions, examples, taxonomy)
2. Retire `classify.py` and `classify_topics.py` — ingest/worker dispatch registers them as `axis_pass` with the config'd axis from data/
3. Same verb surface and job kinds survive (backward compatible)

## Benefit

Axis definitions become self-serve (no Python changes to add/tweak an axis), axis_pass becomes the sole parameterized classifier, and the code-duplication bind breaks.
