"""MaterialHandler — CRC-handbook-style engineering material properties
store with per-value sources (``materials-handbook-kind`` (git-only)).

v1 is **canonical-units-only**: no ``pint``, no unit conversion, no
``units=`` read param, no off-sample estimate/interpolation, no ``model``
value-type — those are deferred follow-ons, not built here.

Two writes share one ``put``, discriminated by whether ``property=`` is
present:

* ``put(kind='material', id=<slug>, title=..., meta={...})`` — upsert the
  material **entity** (a slug ``refs`` row, ``kind='material'``). ``meta``
  carries ``aliases`` / ``material_class`` / ``composition`` / ``notes`` and
  shallow-merges onto an existing entity.
* ``put(kind='material', id=<slug>, property=<prop_id>, value=..., unit=...,
  conditions=..., maturity=..., source=..., chunk=...)`` — append a sourced
  **value** row to ``material_values``. The entity must already exist
  (``material_ref_id`` is handler-enforced to ``kind='material'`` since
  ``refs`` carries no per-kind FK). A unit that isn't the property's
  canonical unit is rejected, naming the canonical one — v1 does no
  conversion. An unknown ``property=`` mints a fresh ``proposed``-tier
  registry row when the call declares a canonical unit (``unit=``, possibly
  ``None`` for a dimensionless quantity) — a ``core`` property is never
  minted this way, only curated by migration.

``get`` renders the handbook page (grouped by property) or, with
``view='properties'``, the registry itself. ``search`` matches
name/alias/class with ``q=``, or does the range-filter read
(``property=/min=/max=/maturity=``, bounds in canonical unit) that is this
kind's reason to exist: "materials with thermal_conductivity < 0.05".

See ``precis-material-help``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from precis.dispatch import Hub, InitError
from precis.errors import BadInput, NotFound
from precis.format import render_agent_table
from precis.handlers._value_entity import (
    _MATURITIES,
    ValueEntityHandler,
)
from precis.handlers._value_entity import (
    display_value as _display_value,
)
from precis.handlers._value_entity import (
    fmt_conditions as _fmt_conditions,
)
from precis.protocol import KindSpec
from precis.response import Response
from precis.utils import handle_registry

#: ``material_values.method`` (migration 0092's comment: "measured |
#: datasheet | dft | estimated | ..."). The column carries no DB CHECK
#: (unlike ``maturity``) — this tuple is the handler-layer enforcement of
#: that documented vocabulary.
_METHODS: tuple[str, ...] = ("measured", "datasheet", "dft", "estimated")
_VIEWS: tuple[str, ...] = ("table", "properties")


class MaterialHandler(ValueEntityHandler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="material",
        title="Material",
        description=(
            "CRC-handbook-style engineering material properties store — a "
            "slug entity (name/aliases/class) plus per-property sourced "
            "values. put(id=<slug>, title=..., meta={...}) upserts the "
            "entity; put(id=<slug>, property=<prop_id>, value=..., unit=..., "
            "conditions=..., maturity=..., source=..., chunk=...) appends a "
            "sourced value — v1 is canonical-unit-only (a non-canonical "
            "unit is rejected, naming the canonical one; no conversion). "
            "get(id=<slug>) is the handbook page grouped by property; "
            "view='properties' lists the registry. "
            "search(property=<prop_id>, min=, max=, maturity=) is the range "
            "filter read; plain q= matches name/alias/class. "
            "See precis-material-help."
        ),
        supports_get=True,
        supports_put=True,
        supports_search=True,
        is_numeric=False,
        id_required=False,
        views=_VIEWS,
    )

    def __init__(self, *, hub: Hub) -> None:
        if hub.store is None:
            raise InitError("material: store required")
        self.store = hub.store

    def accepted_views(self, *, id: Any = None) -> list[str]:
        return list(_VIEWS)

    # ── put ──────────────────────────────────────────────────────────

    def put(
        self,
        *,
        id: str | int | None = None,
        property: str | None = None,
        value: Any = None,
        unit: str | None = None,
        conditions: dict[str, Any] | None = None,
        maturity: str | None = None,
        method: str | None = None,
        source: str | None = None,
        chunk: str | None = None,
        as_of: str | None = None,
        value_type: str | None = None,
        allowed_values: list[Any] | None = None,
        value_low: float | None = None,
        value_high: float | None = None,
        title: str | None = None,
        meta: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Response:
        if id is None or not str(id).strip():
            raise BadInput(
                "put(kind='material') requires id=<slug>",
                next=(
                    "put(kind='material', id='6061-t6', "
                    "title='Aluminum 6061-T6', "
                    "meta={'material_class': 'metal', 'aliases': ['AA6061-T6']})"
                ),
            )
        slug = str(id).strip()
        if property is not None:
            return self._put_value(
                slug,
                key=str(property).strip(),
                value=value,
                unit=unit,
                conditions=conditions,
                maturity=maturity,
                method=method,
                source=source,
                chunk=chunk,
                as_of=as_of,
                value_type=value_type,
                allowed_values=allowed_values,
                value_low=value_low,
                value_high=value_high,
            )
        return self._put_entity(slug, title=title, meta=meta)

    def _put_entity(
        self, slug: str, *, title: str | None, meta: dict[str, Any] | None
    ) -> Response:
        if meta is not None and not isinstance(meta, dict):
            raise BadInput(
                "put(kind='material') meta= must be a dict",
                next=(
                    "put(kind='material', id=..., "
                    "meta={'material_class': 'metal', 'aliases': [...]})"
                ),
            )
        aliases = (meta or {}).get("aliases")
        if aliases is not None and not isinstance(aliases, list):
            raise BadInput("meta.aliases must be a list of strings")
        existing = self.store.get_ref(kind="material", id=slug)
        ttl = (title or (existing.title if existing is not None else slug)).strip()
        ref, created = self.store.material_entity_upsert(
            slug=slug, title=ttl or slug, meta_patch=dict(meta or {})
        )
        verb = "created" if created else "updated"
        rmeta = ref.meta or {}
        lines = [f"{verb} material {slug} ({ref.title})"]
        if rmeta.get("material_class"):
            lines.append(f"class: {rmeta['material_class']}")
        if rmeta.get("aliases"):
            lines.append("aka: " + ", ".join(rmeta["aliases"]))
        return Response(body="\n".join(lines))

    _VE_KIND: ClassVar[str] = "material"
    _VE_ARG: ClassVar[str] = "property"
    _VE_ID: ClassVar[str] = "prop_id"
    _VE_PLURAL: ClassVar[str] = "properties"
    _VE_METHODS: ClassVar[tuple[str, ...]] = _METHODS
    _VE_CREATE_HINT: ClassVar[str] = (
        "put(kind='material', id={slug}, title='...', meta={{'material_class': '...'}})"
    )

    def _ve_registry_row(
        self,
        entity_ref: Any,
        key: str,
        *,
        value: Any,
        unit: str | None,
        value_type: str | None,
        allowed_values: list[Any] | None,
    ) -> dict[str, Any]:
        prop = self.store.material_property_get(key)
        if prop is None:
            return self._mint_property(
                key,
                value=value,
                unit=unit,
                value_type=value_type,
                allowed_values=allowed_values,
            )
        self._check_type_consistency(
            prop, value_type=value_type, allowed_values=allowed_values
        )
        return prop

    def _ve_insert(
        self, entity_ref_id: int, row: Mapping[str, Any], **kwargs: Any
    ) -> int:
        return self.store.material_value_insert(
            material_ref_id=entity_ref_id, property_id=row["prop_id"], **kwargs
        )

    @staticmethod
    def _check_type_consistency(
        prop: dict[str, Any],
        *,
        value_type: str | None,
        allowed_values: list[Any] | None,
    ) -> None:
        """An explicit ``value_type=``/``allowed_values=`` against an
        *already-registered* property must be consistent with it — this
        never re-mints, it only guards against a silently-conflicting
        declaration."""
        if value_type is not None and value_type != prop["value_type"]:
            raise BadInput(
                f"{prop['prop_id']} is already registered as "
                f"value_type={prop['value_type']!r} - value_type={value_type!r} "
                "conflicts with the registered definition",
            )
        if allowed_values is not None:
            registered = prop.get("allowed_values") or []
            if set(allowed_values) != set(registered):
                raise BadInput(
                    f"{prop['prop_id']} is already registered with "
                    f"allowed_values={registered!r} - allowed_values="
                    f"{allowed_values!r} conflicts with the registered definition",
                )

    def _mint_property(
        self,
        prop_id: str,
        *,
        value: Any,
        unit: str | None,
        value_type: str | None = None,
        allowed_values: list[Any] | None = None,
    ) -> dict[str, Any]:
        """Mint a fresh ``proposed`` property when ``property=`` is unknown.

        With no explicit ``value_type=``, a runtime mint infers it from
        ``value=``'s shape (bool -> boolean, numeric -> quantity, else ->
        text) and derives ``dimension`` from the declared ``unit=`` (or
        'dimensionless' when none) — dimension is descriptive-only in v1
        (the unit-conversion follow-on is what actually reads it), so this
        default is safe. ``unit=None`` is a valid declaration (a
        dimensionless quantity/ratio, e.g. poissons_ratio-shaped).

        An explicit ``value_type=`` overrides inference — it's the only way
        to mint a ``categorical`` property (which requires a non-empty
        ``allowed_values=``, and rejects ``unit=`` since categoricals are
        dimensionless).
        """
        if value is None:
            raise NotFound(
                f"unknown property {prop_id!r}",
                next=(
                    "get(kind='material', view='properties') to see the "
                    "registry, or mint a proposed property by writing a "
                    f"value: put(kind='material', id=<slug>, "
                    f"property={prop_id!r}, value=..., unit=<canonical unit "
                    "or omit for dimensionless>)"
                ),
            )
        canonical_unit = None if unit is None else str(unit).strip() or None
        name = prop_id.replace("_", " ").replace("-", " ").strip().title() or prop_id

        if value_type is not None:
            if value_type == "categorical":
                if not allowed_values:
                    raise BadInput(
                        f"minting {prop_id!r} as categorical requires a "
                        "non-empty allowed_values= list",
                        next=(
                            f"put(kind='material', id=<slug>, "
                            f"property={prop_id!r}, value=..., "
                            "value_type='categorical', "
                            "allowed_values=['a', 'b'])"
                        ),
                    )
                if canonical_unit is not None:
                    raise BadInput(
                        f"cannot mint {prop_id!r} as categorical with "
                        f"unit={unit!r} - categorical properties are "
                        "dimensionless, drop unit=",
                    )
                dimension = "categorical"
                canonical_unit = None
            elif value_type in ("boolean", "text"):
                if canonical_unit is not None:
                    raise BadInput(
                        f"cannot mint {prop_id!r} as {value_type} with "
                        f"unit={unit!r} - {value_type} properties are "
                        "dimensionless, drop unit=",
                    )
                dimension = "dimensionless"
            else:  # quantity / ratio
                dimension = canonical_unit or "dimensionless"
            return self.store.material_property_mint(
                prop_id=prop_id,
                name=name,
                canonical_unit=canonical_unit,
                dimension=dimension,
                value_type=value_type,
                allowed_values=allowed_values,
            )

        if isinstance(value, bool):
            if canonical_unit is not None:
                raise BadInput(
                    f"cannot mint {prop_id!r} as boolean with unit={unit!r} - "
                    "boolean properties are dimensionless, drop unit=",
                )
            inferred_type = "boolean"
            dimension = "dimensionless"
        else:
            try:
                float(value)
                inferred_type = "quantity"
            except (TypeError, ValueError):
                inferred_type = "text"
            dimension = canonical_unit or "dimensionless"
        return self.store.material_property_mint(
            prop_id=prop_id,
            name=name,
            canonical_unit=canonical_unit,
            dimension=dimension,
            value_type=inferred_type,
        )

    # ── get ──────────────────────────────────────────────────────────

    def get(
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
        **_kw: Any,
    ) -> Response:
        if view == "properties":
            return self._render_properties()
        if id is None:
            return self._list_materials()
        slug = str(id).strip()
        ref = self.store.get_ref(kind="material", id=slug)
        if ref is None:
            raise NotFound(
                f"material {slug!r} not found",
                next="search(kind='material', q='...') or "
                "get(kind='material') to list every material",
            )
        values = self.store.material_values_for_ref(ref.id)
        if view == "table":
            return self._render_table(ref, values)
        if view is not None:
            raise BadInput(
                f"unknown view={view!r} for kind='material'",
                options=list(_VIEWS),
                next="omit view= for the handbook page",
            )
        return self._render_handbook(ref, values)

    def _list_materials(self) -> Response:
        refs = self.store.list_refs(kind="material", limit=50)
        if not refs:
            return Response(
                body="no materials yet\n\n"
                "Next: put(kind='material', id='6061-t6', "
                "title='Aluminum 6061-T6', meta={'material_class': 'metal'})"
            )
        rows = [
            {
                "id": r.slug or r.id,
                "name": r.title,
                "class": (r.meta or {}).get("material_class") or "—",
            }
            for r in refs
        ]
        return Response(
            body=f"# {len(rows)} material(s)\n"
            + render_agent_table(rows, schema=["id", "name", "class"])
        )

    def _render_properties(self) -> Response:
        props = self.store.material_properties_list()
        rows = [
            {
                "prop_id": p["prop_id"],
                "name": p["name"],
                "unit": p["canonical_unit"] or "—",
                "dimension": p["dimension"] or "—",
                "value_type": p["value_type"],
                "status": p["status"],
            }
            for p in props
        ]
        noun = "property" if len(rows) == 1 else "properties"
        return Response(
            body=f"# {len(rows)} material {noun}\n"
            + render_agent_table(
                rows,
                schema=["prop_id", "name", "unit", "dimension", "value_type", "status"],
            )
        )

    def _render_table(self, ref: Any, values: list[dict[str, Any]]) -> Response:
        if not values:
            return Response(
                body=f"# {ref.title} ({ref.slug}) — no recorded values yet\n\n"
                f"Next: put(kind='material', id={ref.slug!r}, "
                "property='density', value=2700, unit='kg/m3')"
            )
        rows = [
            {
                "property": v["property_id"],
                "value": _display_value(v),
                "conditions": _fmt_conditions(v["conditions"]),
                "maturity": v["maturity"],
                "source": _display_source(v),
            }
            for v in values
        ]
        return Response(
            body=f"# {ref.title} ({ref.slug}) — {len(rows)} value(s)\n"
            + render_agent_table(
                rows, schema=["property", "value", "conditions", "maturity", "source"]
            )
        )

    def _render_handbook(self, ref: Any, values: list[dict[str, Any]]) -> Response:
        meta = ref.meta or {}
        lines = [f"# material {ref.slug}: {ref.title}"]
        if meta.get("material_class"):
            lines.append(f"class: {meta['material_class']}")
        if meta.get("aliases"):
            lines.append("aka: " + ", ".join(meta["aliases"]))
        if meta.get("composition"):
            lines.append(f"composition: {meta['composition']}")
        if meta.get("notes"):
            lines.append(f"notes: {meta['notes']}")
        if not values:
            lines += [
                "",
                "no recorded values yet",
                "",
                f"Next: put(kind='material', id={ref.slug!r}, "
                "property='density', value=2700, unit='kg/m3')",
            ]
            return Response(body="\n".join(lines))

        grouped: dict[str, list[dict[str, Any]]] = {}
        for v in values:
            grouped.setdefault(v["property_id"], []).append(v)

        lines.append("")
        for prop_id, vs in grouped.items():
            prop = self.store.material_property_get(prop_id) or {}
            unit = prop.get("canonical_unit")
            header = f"## {prop.get('name') or prop_id} ({prop_id})"
            if unit:
                header += f" [{unit}]"
            lines.append(header)
            for v in vs:
                bits = [f"- {_display_value(v)}"]
                cond = _fmt_conditions(v["conditions"])
                if cond:
                    bits.append(f"conditions={cond}")
                bits.append(f"maturity={v['maturity']}")
                bits.append(f"source={_display_source(v)}")
                lines.append("  ".join(bits))
            lines.append("")
        return Response(body="\n".join(lines).rstrip() + "\n")

    # ── search ───────────────────────────────────────────────────────

    def search(
        self,
        *,
        q: str | None = None,
        property: str | None = None,
        min: float | None = None,
        max: float | None = None,
        maturity: str | None = None,
        page_size: int = 20,
        **_kw: Any,
    ) -> Response:
        if property is not None:
            return self._search_values(
                property=str(property).strip(),
                min_val=min,
                max_val=max,
                maturity=maturity,
                limit=page_size,
            )
        if q is None or not str(q).strip():
            raise BadInput(
                "search(kind='material') requires q=, or the "
                "property=/min=/max=/maturity= range filter",
                next="search(kind='material', q='aluminum') or "
                "search(kind='material', property='thermal_conductivity', max=0.05)",
            )
        return self._search_entities(str(q).strip(), limit=page_size)

    def _search_entities(self, q: str, *, limit: int) -> Response:
        hits = self.store.material_search_entities(q, limit=limit)
        if not hits:
            return Response(body=f"no material entries match {q!r}")
        rows = [
            {
                "id": slug or ref_id,
                "name": title,
                "class": (meta or {}).get("material_class") or "—",
            }
            for ref_id, slug, title, meta in hits
        ]
        return Response(
            body=f"# {len(rows)} material(s) matching {q!r}\n"
            + render_agent_table(rows, schema=["id", "name", "class"])
        )

    def _search_values(
        self,
        *,
        property: str,
        min_val: float | None,
        max_val: float | None,
        maturity: str | None,
        limit: int,
    ) -> Response:
        if not property:
            raise BadInput(
                "search(kind='material') property= needs a non-empty prop_id",
                next="get(kind='material', view='properties')",
            )
        prop = self.store.material_property_get(property)
        if prop is None:
            raise NotFound(
                f"unknown property {property!r}",
                next="get(kind='material', view='properties')",
            )
        if (min_val is not None or max_val is not None) and prop["value_type"] not in (
            "quantity",
            "ratio",
        ):
            raise BadInput(
                f"{property!r} is a {prop['value_type']!r} property - "
                "min=/max= apply only to numeric (quantity/ratio) properties",
                next=f"search(kind='material', property={property!r}) "
                "without min=/max=, or search(kind='material', q='...')",
            )
        if maturity is not None and maturity not in _MATURITIES:
            raise BadInput(f"maturity={maturity!r} must be one of {list(_MATURITIES)}")
        hits = self.store.material_search_values(
            property_id=property,
            min_val=min_val,
            max_val=max_val,
            maturity=maturity,
            limit=limit,
        )
        if not hits:
            return Response(
                body=f"no material values match property={property!r} "
                f"min={min_val!r} max={max_val!r} maturity={maturity!r}"
            )
        unit = prop.get("canonical_unit") or ""
        rows = [
            {
                "material": v["material_title"],
                "value": _display_value(v) + (f" {unit}" if unit else ""),
                "conditions": _fmt_conditions(v["conditions"]),
                "maturity": v["maturity"],
                "source": _display_source(v),
            }
            for v in hits
        ]
        return Response(
            body=f"# {len(rows)} value(s) for {property!r}"
            + (f" (unit={unit})" if unit else "")
            + "\n"
            + render_agent_table(
                rows, schema=["material", "value", "conditions", "maturity", "source"]
            )
        )


def _display_source(v: dict[str, Any]) -> str:
    ref_id = v.get("source_ref_id")
    kind = v.get("source_kind")
    if ref_id is not None and kind:
        handle = handle_registry.try_format(kind, ref_id) or f"{kind}:{ref_id}"
        chunk = v.get("source_chunk")
        return f"{handle}~{chunk}" if chunk else handle
    if v.get("source_url"):
        return str(v["source_url"])
    return "—"


__all__ = ["MaterialHandler"]
