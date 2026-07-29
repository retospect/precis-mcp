"""MaterialHandler — CRC-handbook-style engineering material properties
store with per-value sources (docs/proposals/materials-handbook-kind.md).

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

from typing import Any, ClassVar

from precis.dispatch import Hub, InitError
from precis.errors import BadInput, NotFound
from precis.format import render_agent_table
from precis.handlers._link_target import LinkTarget, parse_link_target
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.utils import handle_registry

_MATURITIES: tuple[str, ...] = ("commercial", "lab", "speculative")
_SOURCE_KINDS: tuple[str, ...] = ("paper", "datasheet")
_VIEWS: tuple[str, ...] = ("table", "properties")
_VALUE_TYPES: tuple[str, ...] = ("quantity", "ratio", "categorical", "boolean", "text")


class MaterialHandler(Handler):
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

    def put(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        property: str | None = None,
        value: Any = None,
        unit: str | None = None,
        conditions: dict[str, Any] | None = None,
        maturity: str | None = None,
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
                property=str(property).strip(),
                value=value,
                unit=unit,
                conditions=conditions,
                maturity=maturity,
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

    def _put_value(
        self,
        slug: str,
        *,
        property: str,
        value: Any,
        unit: str | None,
        conditions: dict[str, Any] | None,
        maturity: str | None,
        source: str | None,
        chunk: str | None,
        as_of: str | None = None,
        value_type: str | None = None,
        allowed_values: list[Any] | None = None,
        value_low: float | None = None,
        value_high: float | None = None,
    ) -> Response:
        if not property:
            raise BadInput(
                "put(kind='material') with property= needs a non-empty prop_id",
                next="get(kind='material', view='properties') to see the registry",
            )
        material_ref = self.store.get_ref(kind="material", id=slug)
        if material_ref is None:
            raise NotFound(
                f"material {slug!r} not found - create the entity first",
                next=(
                    f"put(kind='material', id={slug!r}, "
                    "title='...', meta={'material_class': '...'})"
                ),
            )
        if conditions is not None and not isinstance(conditions, dict):
            raise BadInput("put(kind='material') conditions= must be a dict")

        self._validate_type_args(value_type, allowed_values)

        prop = self.store.material_property_get(property)
        if prop is None:
            prop = self._mint_property(
                property,
                value=value,
                unit=unit,
                value_type=value_type,
                allowed_values=allowed_values,
            )
        else:
            self._check_type_consistency(
                prop, value_type=value_type, allowed_values=allowed_values
            )

        self._check_unit(prop, unit)
        value_kwargs = self._route_value(
            prop, value, value_low=value_low, value_high=value_high
        )

        if maturity is not None and maturity not in _MATURITIES:
            raise BadInput(
                f"maturity={maturity!r} must be one of {list(_MATURITIES)}",
                next=f"put(kind='material', id={slug!r}, property={property!r}, "
                f"value=..., maturity='lab')",
            )

        source_ref_id, source_chunk, source_url = self._resolve_source(source, chunk)

        value_id = self.store.material_value_insert(
            material_ref_id=material_ref.id,
            property_id=prop["prop_id"],
            conditions=conditions,
            maturity=maturity or "lab",
            source_ref_id=source_ref_id,
            source_chunk=source_chunk,
            source_url=source_url,
            as_of=as_of,
            **value_kwargs,
        )
        display_value = _display_value(
            {
                "value_num": value_kwargs.get("value_num"),
                "value_low": value_kwargs.get("value_low"),
                "value_high": value_kwargs.get("value_high"),
                "value_bool": value_kwargs.get("value_bool"),
                "value_text": value_kwargs.get("value_text"),
            }
        )
        unit_note = f" {unit}" if unit else ""
        source_note = ""
        if source_ref_id is not None:
            source_note = f" (source={source!r})"
        elif source_url is not None:
            source_note = f" (source_url={source_url!r})"
        return Response(
            body=(
                f"recorded {slug}.{prop['prop_id']} = {display_value}{unit_note} "
                f"(id={value_id}, maturity={maturity or 'lab'})"
                f"{source_note}"
            )
        )

    @staticmethod
    def _validate_type_args(
        value_type: str | None, allowed_values: list[Any] | None
    ) -> None:
        """Validate ``value_type=``/``allowed_values=`` shape, independent
        of whether this write mints a fresh property or targets an existing
        one (``_check_type_consistency`` covers the latter)."""
        if value_type is not None and value_type not in _VALUE_TYPES:
            raise BadInput(
                f"value_type={value_type!r} must be one of {list(_VALUE_TYPES)}",
            )
        if allowed_values is not None and value_type != "categorical":
            raise BadInput(
                "allowed_values= is only valid with value_type='categorical'",
                next=(
                    "put(kind='material', id=<slug>, property=<prop_id>, "
                    "value=..., value_type='categorical', "
                    "allowed_values=['a', 'b'])"
                ),
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

    @staticmethod
    def _check_unit(prop: dict[str, Any], unit: str | None) -> None:
        canonical = prop.get("canonical_unit")
        given = None if unit is None else (str(unit).strip() or None)
        if canonical is not None:
            if given != canonical:
                raise BadInput(
                    f"unit={unit!r} is not {prop['prop_id']}'s canonical "
                    f"unit ({canonical!r}) - v1 is canonical-unit-only, "
                    "no conversion",
                    next=(
                        f"put(kind='material', id=<slug>, "
                        f"property={prop['prop_id']!r}, value=..., "
                        f"unit={canonical!r})"
                    ),
                )
        elif given is not None:
            raise BadInput(
                f"{prop['prop_id']} has no canonical unit "
                "(dimensionless/categorical/boolean/text) - drop unit=",
                next=(
                    f"put(kind='material', id=<slug>, "
                    f"property={prop['prop_id']!r}, value=...)"
                ),
            )

    @staticmethod
    def _route_value(
        prop: dict[str, Any],
        value: Any,
        *,
        value_low: float | None = None,
        value_high: float | None = None,
    ) -> dict[str, Any]:
        prop_id = prop["prop_id"]
        value_type = prop["value_type"]
        has_band = value_low is not None or value_high is not None

        if has_band and value_type not in ("quantity", "ratio"):
            raise BadInput(
                f"{prop_id} is a {value_type} property - value_low=/"
                "value_high= apply only to numeric (quantity/ratio) properties",
            )

        if value_type in ("quantity", "ratio"):
            if (
                value_low is not None
                and value_high is not None
                and value_low > value_high
            ):
                raise BadInput(
                    f"value_low={value_low!r} must be <= value_high={value_high!r}",
                )
            if value is not None:
                if isinstance(value, bool):
                    raise BadInput(
                        f"{prop_id} is a {value_type} property - value= must be "
                        f"numeric, got {value!r}"
                    )
                try:
                    num = float(value)
                except (TypeError, ValueError):
                    raise BadInput(
                        f"{prop_id} is a {value_type} property - value= must be "
                        f"numeric, got {value!r}"
                    ) from None
            elif value_low is not None and value_high is not None:
                num = (float(value_low) + float(value_high)) / 2
            elif has_band:
                raise BadInput(
                    f"put(kind='material', property={prop_id!r}) needs "
                    "value=, or both value_low= and value_high=",
                )
            else:
                raise BadInput(
                    f"put(kind='material', property={prop_id!r}) needs value=",
                )
            out: dict[str, Any] = {"value_num": num}
            if value_low is not None:
                out["value_low"] = float(value_low)
            if value_high is not None:
                out["value_high"] = float(value_high)
            return out
        if value is None:
            raise BadInput(
                f"put(kind='material', property={prop_id!r}) needs value=",
            )
        if value_type == "boolean":
            b = _coerce_bool(value)
            if b is None:
                raise BadInput(
                    f"{prop_id} is boolean - value= must be true/false, got {value!r}"
                )
            return {"value_bool": b}
        if value_type == "categorical":
            s = str(value).strip()
            allowed = prop.get("allowed_values") or []
            if allowed and s not in allowed:
                raise BadInput(
                    f"{prop_id} value {s!r} is not in allowed_values {allowed!r}",
                    next=f"pick one of {allowed!r}",
                )
            return {"value_text": s}
        # text
        return {"value_text": str(value).strip()}

    def _resolve_source(
        self, source: str | None, chunk: str | None
    ) -> tuple[int | None, str | None, str | None]:
        """Resolve ``source=``/``chunk=`` to ``(source_ref_id, source_chunk,
        source_url)``. Mirrors ``citation``'s validation depth: the
        referenced ref must exist; a resolvable ``pc<id>`` universal handle
        for ``chunk=`` is normalised, a bare ordinal/handle is stored as
        given (no hard resolution requirement, same leniency citation
        applies)."""
        if source is None or not str(source).strip():
            if chunk is not None:
                raise BadInput(
                    "chunk= requires source= (the ref the chunk belongs to)",
                    next="put(kind='material', id=<slug>, property=..., "
                    "value=..., source='paper:<slug>', chunk='<slug>~5')",
                )
            return None, None, None
        s = str(source).strip()
        if s.lower().startswith("http://") or s.lower().startswith("https://"):
            if chunk is not None:
                raise BadInput(
                    "chunk= is only meaningful with a ref source= "
                    "('paper:<slug>' / a handle), not a bare source_url",
                )
            return None, None, s
        target = parse_link_target(s, store=self.store)
        if target.kind not in _SOURCE_KINDS:
            raise BadInput(
                f"source={source!r} resolves to kind={target.kind!r}; "
                f"material sources must be one of {list(_SOURCE_KINDS)}, or "
                "a bare http(s) URL",
            )
        source_chunk: str | None = None
        if chunk is not None:
            c = str(chunk).strip()
            if handle_registry.parse(c) is not None:
                resolved = self.store.resolve_handle(c)
                if resolved is not None and resolved.chunk_ord is not None:
                    if resolved.ref_id != target.ref_id:
                        src_public = self._source_public_id(target)
                        raise BadInput(
                            f"chunk={chunk!r} belongs to {resolved.public_id!r}, "
                            f"not source={source!r} ({src_public!r})",
                            next="pass source= and chunk= from the same ref, "
                            "or drop one of them",
                        )
                    c = f"{resolved.public_id}~{resolved.chunk_ord}"
            source_chunk = c
        elif target.pos is not None:
            # source= itself was a chunk-level handle (e.g. 'pc<id>') with no
            # separate chunk= — record that chunk instead of dropping to ref
            # granularity.
            source_chunk = f"{self._source_public_id(target)}~{target.pos}"
        return target.ref_id, source_chunk, None

    def _source_public_id(self, target: LinkTarget) -> str:
        ref = self.store.get_ref(kind=target.kind, id=target.ref_id)
        return ref.public_id if ref is not None else str(target.ref_id)

    # ── get ──────────────────────────────────────────────────────────

    def get(  # type: ignore[override]
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

    def search(  # type: ignore[override]
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


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("true", "yes", "1"):
            return True
        if s in ("false", "no", "0"):
            return False
    return None


def _display_value(v: dict[str, Any]) -> str:
    if v["value_num"] is not None:
        base = str(v["value_num"])
        low = v.get("value_low")
        high = v.get("value_high")
        if low is not None and high is not None:
            return f"{base} ({low}–{high})"
        return base
    if v["value_bool"] is not None:
        return str(v["value_bool"])
    if v["value_text"] is not None:
        return v["value_text"]
    return "—"


def _fmt_conditions(conditions: Any) -> str:
    if not conditions:
        return ""
    return ", ".join(f"{k}={v}" for k, v in conditions.items())


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
