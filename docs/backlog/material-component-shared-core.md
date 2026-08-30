# material/component: whole-handler fork → shared typed-property core

`handlers/material.py` ↔ `handlers/component.py` share 20 symbols outright. ~500 LOC true duplication.

## Shared handler methods (20 symbols)

_check_type_consistency, _check_unit, _coerce_bool, _display_source, _display_value,
_fmt_conditions, _put_entity, _put_value, _render_table, _resolve_source, _route_value,
_search_entities, _search_values, _source_public_id, _validate_type_args, accepted_views,
get, put, search, __init__

## Store-layer duplication

`store/_material_ops.py` vs `store/_component_ops.py` — same op set, component adds containment.

## Deviation: _resolve_source

Byte-identical except material→component in one error string. Component copy has already lost an explanatory comment (documentation drift indicator).

## Right shape

A shared `TypedPropertyMixin` (or similar) — the two kinds parameterize a base for:
- Typed property with unit, conditions, maturity, source
- Value storage and search
- Rendering and entity resolution
- Source attribution and lineage

Split the handler into `BaseTypedPropertyHandler` (shared) + per-kind subclasses (overrides). Do the same at the store layer with parameterized ops.

Prerequisite: clarify whether component's added containment belongs in the base or as a component-only override.
