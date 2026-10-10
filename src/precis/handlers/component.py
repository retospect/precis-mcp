"""ComponentHandler — a general procurable-part store: bolts, hoses, pipes,
beams, gaskets, bearings, adhesives, electronic components
(``component-kind`` (git-only)).

Mirrors ``material``'s star schema (entity + typed registry + sourced value
fact table, canonical-units-only) but adds a **category dimension**: every
component entity declares a `category=` (fastener/hose/pipe/.../proposed),
and specs (the material-property analogue) are optionally scoped to one
category (``None`` = universal, applies to any component).

Two writes share one ``put``, discriminated by whether ``spec=`` is present:

* ``put(kind='component', id=<slug>, title=..., category=..., uom=...,
  meta={...})`` — upsert the component **entity**. ``category=`` is
  required on create; an unknown category mints a fresh ``proposed`` row in
  the category registry (never silently ``core``). ``meta`` carries
  ``mpn``/``manufacturer``/``sku``/``package``/``aliases``/``notes`` and
  shallow-merges onto an existing entity, same as ``material``.
* ``put(kind='component', id=<slug>, spec=<spec_id>, value=..., unit=...,
  conditions=..., maturity=..., source=..., chunk=..., as_of=...)`` —
  append a sourced **value** row to ``component_spec_values``. The entity
  must already exist. A spec not applicable to the component's category is
  rejected, naming the category; an unknown numeric ``spec=`` mints a fresh
  ``proposed`` spec **scoped to that component's category** (never
  universal).
* ``put(kind='component', id=<slug>, made_of='material:<slug>')`` — on any
  put that carries ``made_of=``, resolve it to a ``material`` ref (rejected
  if it isn't one) and create the ``made-of`` link.
* ``put(kind='component', id=<parent slug>, contains=<child component ref>,
  qty=<int>=0, ref_designator=...)`` — the assembly tree (BOM edge,
  ``component-assembly-tree`` (git-only)): ``contains=`` resolves to a
  ``component`` ref; ``qty=0`` removes the edge; ``qty`` omitted on an
  existing edge preserves its current quantity; a cycle (self or transitive
  ancestor) is rejected.
* ``put(kind='component', series=<series_id>, size='M6x30')`` — **mint from
  the standards series registry** (:mod:`precis.component_series`,
  ``se-off-the-shelf-fabrication.md`` engine 1): materialize one size of a
  published family (ISO 4762, EN 10255, …) into an entity plus its dimension
  values, instead of hand-entering four hundred screws. ``id=`` is optional
  (a deterministic slug is derived, so two agents minting the same part
  converge on one ref); the values land as ordinary
  ``component_spec_values`` rows with ``method='standard'``, and a spec
  already carrying that value is skipped rather than re-appended.

``get`` renders the component page (specs grouped by spec, plus entity
facts and the made-of material), ``view='table'`` (tidy one-row-per-value),
``view='specs'`` (universal + this component's category specs),
``view='categories'`` (the category registry), ``view='tree'`` (the nested
assembly tree), or ``view='bom'`` (the flattened BOM with cost/mass rollup,
optionally annotated by ``spec=`` for a cross-leaf consistency check), or
``view='series'`` (the standards registry — bare = the index, ``id=`` = one
family's size table, ``q=`` = the ranked colloquial resolver), or
``view='stock'`` (is it buyable: the curated availability tier from the
series row, plus a live supplier quote when a stock adapter is configured —
:mod:`precis.supply`).
``search`` matches name/alias/mpn/manufacturer/category with ``q=``, or
does the range-filter read (``spec=/min=/max=/maturity=``, optionally
narrowed by ``category=``).

See ``precis-component-help``.

Explicit ``mode=supplier-link`` persists only confirmed supplier identity and
URLs. API-data import was refused under the licence; values come instead from
independently ingested manufacturer datasheets. Single-component MCP pages
fetch uncached supplier records live by default; ``view=stored`` and all bulk
surfaces remain offline. The web item page requests live data lazily.

"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any, ClassVar

from precis import component_series as cseries
from precis import supply
from precis.dispatch import Hub, InitError
from precis.errors import BadInput, NotFound
from precis.format import render_agent_table
from precis.handlers._link_tag_ops import (
    apply_link_ops,
    format_link_tag_ack,
    require_link_target,
    validate_link_mode,
)
from precis.handlers._link_target import parse_link_target
from precis.handlers._slug_ref_shared import resolve_live_slug_ref
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
from precis.store._component_ops import (
    ComponentCategoryRow,
    ComponentSpecRow,
    ComponentValueRowWithSource,
)
from precis.supply.catalog import series_mapping, validate_filters
from precis.utils import handle_registry

#: ``component_spec_values.method`` (migration 0093's comment: "measured |
#: datasheet | estimated | ..."), plus ``standard`` — already written
#: (unvalidated) by the series-mint path (``_write_series_specs`` below).
#: The column carries no DB CHECK (unlike ``maturity``) — this tuple is the
#: handler-layer enforcement of that vocabulary.
_METHODS: tuple[str, ...] = ("measured", "datasheet", "estimated", "standard")
_VIEWS: tuple[str, ...] = (
    "table",
    "specs",
    "categories",
    "tree",
    "bom",
    "series",
    "stock",
    "supplier-categories",
    "stored",
    "supplier-live",
)


class ComponentHandler(ValueEntityHandler):
    #: args= keys each verb reads out of ``args`` (gr475332); dispatch
    #: rejects any other key. Keep in step with the verb bodies.
    ARGS_KEYS: ClassVar[dict[str, frozenset[str]]] = {
        "put": frozenset({"supplier_part_number"}),
        "get": frozenset({"category_id", "parameters"}),
    }

    spec: ClassVar[KindSpec] = KindSpec(
        kind="component",
        title="Component",
        description=(
            "General procurable-part store (bolt/hose/pipe/bearing/...) — "
            "a slug entity (name/category/mpn/manufacturer) plus "
            "per-spec sourced values. put(id=<slug>, title=..., "
            "category=..., uom=..., meta={...}) upserts the entity "
            "(category= required, mints a proposed category if unknown); "
            "put(id=<slug>, spec=<spec_id>, value=..., unit=..., "
            "conditions=..., maturity=..., source=..., chunk=..., "
            "as_of=...) appends a sourced value — canonical-unit-only, "
            "spec must apply to the component's category. "
            "put(id=<slug>, made_of='material:<slug>') links the material "
            "it's made of; put(id=<parent slug>, contains=<child component "
            "ref>, qty=<int>=0, ref_designator=...) writes an assembly-tree "
            "edge (qty=0 removes it). get(id=<slug>) is the component "
            "page; view='specs'/'categories' list the registries; "
            "view='tree' is the nested assembly tree; view='bom' is the "
            "flattened BOM with cost/mass rollup (add spec=<spec_id> for a "
            "cross-leaf consistency check). "
            "put(id=<slug>, series=<series_id>, size='M6x30') MINTS the "
            "entity from the standards series registry (id= optional — a "
            "deterministic slug is derived). get(view='series') lists the "
            "series; view='series' with id=<series_id> is its size table, "
            "with q='<colloquial>' the ranked resolver; view='stock' is the "
            "availability tier plus a live supplier quote. "
            "search(spec=<spec_id>, min=, max=, maturity=, category=) is "
            "the range filter read; plain q= matches name/mpn/manufacturer/"
            "category. See precis-component-help."
        ),
        supports_get=True,
        supports_put=True,
        supports_search=True,
        supports_link=True,
        is_numeric=False,
        id_required=False,
        views=_VIEWS,
    )

    def __init__(self, *, hub: Hub) -> None:
        if hub.store is None:
            raise InitError("component: store required")
        self.store = hub.store
        self.hub = hub

    def accepted_views(self, *, id: Any = None) -> list[str]:
        return list(_VIEWS)

    # ── put ──────────────────────────────────────────────────────────

    def put(
        self,
        *,
        id: str | int | None = None,
        spec: str | None = None,
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
        category: str | None = None,
        uom: str | None = None,
        made_of: str | None = None,
        contains: str | None = None,
        qty: int | None = None,
        ref_designator: str | None = None,
        meta: dict[str, Any] | None = None,
        series: str | None = None,
        size: str | None = None,
        mode: str | None = None,
        args: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Response:
        if mode == "supplier-link":
            if (
                source not in {"digikey", "farnell", "mouser"}
                or spec is not None
                or series is not None
                or size is not None
            ):
                raise BadInput(
                    "supplier-link requires a supplier source and no spec/series/size"
                )
            return self._put_supplier_link(id=id, source=source, args=args)
        if mode is not None:
            raise BadInput(
                "component mode must be omitted or 'supplier-link'; API data import is not supported"
            )
        if series is not None or size is not None:
            return self._mint_from_series(
                id=id,
                series=series,
                size=size,
                title=title,
                uom=uom,
                meta=meta,
            )
        if id is None or not str(id).strip():
            raise BadInput(
                "put(kind='component') requires id=<slug>",
                next=(
                    "put(kind='component', id='m6-a2-bolt', "
                    "title='M6x20 A2 socket cap', category='fastener', "
                    "uom='each', meta={'mpn': '...'})"
                ),
            )
        slug = str(id).strip()

        if spec is not None:
            resp = self._put_value(
                slug,
                key=str(spec).strip(),
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
        else:
            resp = self._put_entity(
                slug, title=title, category=category, uom=uom, meta=meta
            )

        if made_of is not None:
            resp = self._put_made_of(slug, made_of=str(made_of).strip(), base=resp)
        if contains is not None:
            resp = self._put_contains(
                slug,
                contains=str(contains).strip(),
                qty=qty,
                ref_designator=ref_designator,
                base=resp,
            )
        return resp

    def _put_supplier_link(
        self, *, id: str | int | None, source: str, args: dict[str, Any] | None
    ) -> Response:
        import hashlib

        from precis.supply.base import now
        from precis.supply.live import identity_link, supplier_adapter

        if id is None:
            raise BadInput("supplier-link requires an existing component id")
        if args is not None and (
            not isinstance(args, dict) or set(args) - {"supplier_part_number"}
        ):
            raise BadInput("supplier-link args accept supplier_part_number only")
        ref = self.store.get_ref(kind="component", id=str(id).strip())
        if ref is None:
            raise NotFound("supplier-link requires an existing component")
        confirmed = (args or {}).get("supplier_part_number")
        if confirmed is not None and (
            not isinstance(confirmed, str) or not confirmed.strip()
        ):
            raise BadInput("supplier_part_number must be a nonempty string")
        mpn = str(
            (ref.meta or {}).get("mpn") or (ref.meta or {}).get("designation") or ""
        ).strip()
        if not mpn and not confirmed:
            raise BadInput(
                "supplier-link needs recorded MPN or confirmed supplier part number"
            )
        adapter = supplier_adapter(source)
        try:
            why = adapter.configured()
            if why:
                raise BadInput(why)
            record = adapter.record(mpn, supplier_part_number=confirmed)
            link = identity_link(source, record, now())
        except Exception as exc:
            from precis.supply.live import safe_error

            raise BadInput(f"supplier link refused: {safe_error(exc)}") from None
        changed = self.store.component_supplier_link(ref.id, link)
        note = "no datasheet URL supplied"
        if link["datasheet_url"]:
            job = self.hub.sibling("job")
            queued = job.put(
                job_type="datasheet_pull",
                executor="job_inproc",
                parent_id=ref.id,
                params={"component_ref_id": ref.id, "url": link["datasheet_url"]},
                idem_key=f"datasheet_pull:component:{ref.id}:"
                + hashlib.sha256(link["datasheet_url"].encode()).hexdigest()[:16],
            )
            note = "manufacturer datasheet queued" + (
                " (existing job)" if queued.reused else ""
            )
        return Response(
            body=f"{'linked' if changed else 'unchanged'} {source} {link['supplier_part_number']} · {note}",
            ref_id=ref.id,
            reused=not changed,
        )

    def _put_entity(
        self,
        slug: str,
        *,
        title: str | None,
        category: str | None,
        uom: str | None,
        meta: dict[str, Any] | None,
    ) -> Response:
        if meta is not None and not isinstance(meta, dict):
            raise BadInput(
                "put(kind='component') meta= must be a dict",
                next=(
                    "put(kind='component', id=..., category='fastener', "
                    "meta={'mpn': '...', 'manufacturer': '...'})"
                ),
            )
        aliases = (meta or {}).get("aliases")
        if aliases is not None and not isinstance(aliases, list):
            raise BadInput("meta.aliases must be a list of strings")

        existing = self.store.get_ref(kind="component", id=slug)
        if existing is None and not category:
            raise BadInput(
                "put(kind='component') requires category= when creating the entity",
                next=(
                    "put(kind='component', id='m6-a2-bolt', "
                    "title='M6x20 A2 socket cap', category='fastener')"
                ),
            )

        meta_patch = dict(meta or {})
        if category is not None:
            cat = self._resolve_category(str(category).strip())
            meta_patch["category"] = cat["category_id"]
        if uom is not None:
            meta_patch["uom"] = str(uom).strip()

        ttl = (title or (existing.title if existing is not None else slug)).strip()
        ref, created = self.store.component_entity_upsert(
            slug=slug, title=ttl or slug, meta_patch=meta_patch
        )
        verb = "created" if created else "updated"
        rmeta = ref.meta or {}
        lines = [f"{verb} component {slug} ({ref.title})"]
        if rmeta.get("category"):
            lines.append(f"category: {rmeta['category']}")
        if rmeta.get("uom"):
            lines.append(f"uom: {rmeta['uom']}")
        if rmeta.get("mpn"):
            lines.append(f"mpn: {rmeta['mpn']}")
        if rmeta.get("aliases"):
            lines.append("aka: " + ", ".join(rmeta["aliases"]))
        return Response(body="\n".join(lines))

    def _resolve_category(self, category_id: str) -> ComponentCategoryRow:
        """Look up ``category_id``, minting a fresh ``proposed`` row when
        it's unknown (never silently ``core`` — that tier is curated by
        migration only)."""
        cat = self.store.component_category_get(category_id)
        if cat is not None:
            return cat
        name = category_id.replace("_", " ").replace("-", " ").strip().title()
        return self.store.component_category_mint(
            category_id=category_id, name=name or category_id
        )

    # -- mint from the standards series registry -------------------------

    def _mint_from_series(
        self,
        *,
        id: str | int | None,
        series: str | None,
        size: str | None,
        title: str | None,
        uom: str | None,
        meta: dict[str, Any] | None,
    ) -> Response:
        """Materialize one size of a standards series into a real entity
        plus its dimension values (``component_series.py``).

        Idempotent by construction: the entity upsert is an upsert, and a
        spec whose *current* value already equals what the series would
        write is skipped rather than re-appended — re-minting the same
        part twice must not silently grow the fact table, which is
        append-only and where a BOM rollup reads its one current value
        from."""
        if not series:
            raise BadInput(
                "put(kind='component', size=...) also needs series=",
                next=(
                    "get(kind='component', view='series') to list the "
                    "series, then put(kind='component', series='iso-4762', "
                    "size='M6x30')"
                ),
            )
        srs = cseries.find_series(str(series).strip())
        if srs is None:
            known = ", ".join(s.series_id for s in cseries.load_series())
            raise BadInput(
                f"unknown series {series!r}; known: {known}",
                next="get(kind='component', view='series') for the registry",
            )
        if not size:
            raise BadInput(
                f"put(kind='component', series={srs.series_id!r}) needs size=",
                next=(
                    f"get(kind='component', id={srs.series_id!r}, "
                    "view='series') for the size table"
                ),
            )
        size_key, length = cseries.split_designation(str(size).strip())
        row = srs.size(size_key)
        if row is None:
            keys = ", ".join(s.key for s in srs.sizes)
            raise BadInput(
                f"{srs.series_id} has no size {size_key!r}; sizes: {keys}",
                next=(
                    f"get(kind='component', id={srs.series_id!r}, "
                    "view='series') for the size table"
                ),
            )
        warn = cseries.check_length(srs, row, length)
        if warn is not None and length is None and srs.length_spec is not None:
            # A missing length on a series that has one is a hard miss, not
            # an advisory: the part would be dimensionless.
            raise BadInput(
                warn,
                next=(
                    f"put(kind='component', series={srs.series_id!r}, "
                    f"size='{row.key}x20')"
                ),
            )

        slug = (
            str(id).strip()
            if id is not None and str(id).strip()
            else cseries.suggest_slug(srs, row, length)
        )
        entity_meta = dict(meta or {})
        entity_meta["series"] = srs.series_id
        entity_meta["size"] = row.key if length is None else f"{row.key}x{length:g}"
        if srs.designation:
            entity_meta["designation"] = srs.designation
        resp = self._put_entity(
            slug,
            title=title or cseries.title_for(srs, row, length),
            category=srs.category,
            uom=uom or "each",
            meta=entity_meta,
        )
        ref = self.store.get_ref(kind="component", id=slug)
        assert ref is not None  # _put_entity just upserted it

        specs = cseries.mint_specs(srs, row, length)
        written, skipped, missing = self._write_series_specs(
            ref.id, specs, category=srs.category, source=srs.source
        )
        lines = [resp.body, f"series: {srs.series_id} ({srs.source})"]
        lines.append(
            f"specs: {written} written, {skipped} already current"
            + (f", {len(missing)} skipped ({'; '.join(missing)})" if missing else "")
        )
        if warn is not None:
            lines.append(f"⚠ {warn}")
        lines.append(f"Next: get(kind='component', id='{slug}')")
        return Response(body="\n".join(lines))

    def _write_series_specs(
        self,
        ref_id: int,
        specs: dict[str, Any],
        *,
        category: str,
        source: str,
    ) -> tuple[int, int, list[str]]:
        """Write the series' dimensions as ordinary sourced values.

        Returns ``(written, already_current, unconvertible_or_unknown)``.
        A spec the registry doesn't know — or one whose canonical unit
        this file's millimetre convention can't reach — is *reported*,
        never written: the series file is curated data, so a miss there is
        a data bug to fix in the file, and either silently minting a
        ``proposed`` spec or writing an unconverted number would bury
        it."""
        written = 0
        skipped = 0
        missing: list[str] = []
        for spec_id in sorted(specs):
            spec_row = self.store.component_spec_get(spec_id)
            if spec_row is None:
                missing.append(spec_id)
                continue
            self._check_applicability(spec_row, category)
            raw = specs[spec_id]
            if spec_row["value_type"] in ("quantity", "ratio"):
                converted, complaint = cseries.to_canonical(
                    float(raw),
                    canonical_unit=spec_row["canonical_unit"],
                    dimension=spec_row["dimension"],
                )
                if complaint is not None:
                    missing.append(f"{spec_id} ({complaint})")
                    continue
                raw = converted
            routed = self._route_value(spec_row, raw)
            current = self.store.component_current_spec_value(ref_id, spec_id)
            if current is not None and all(
                current.get(k) == v for k, v in routed.items()
            ):
                skipped += 1
                continue
            self.store.component_value_insert(
                component_ref_id=ref_id,
                spec_id=spec_id,
                maturity="commercial",
                method="standard",
                notes=source,
                **routed,
            )
            written += 1
        return written, skipped, missing

    # -- series registry views (view='series') ---------------------------

    def _render_series(self, *, id: str | None, q: str | None) -> Response:
        if id:
            return self._render_one_series(id)
        if q:
            return self._render_series_resolve(q)
        rows = [
            {
                "series": s.series_id,
                "designation": s.designation or "—",
                "category": s.category,
                "sizes": str(len(s.sizes)),
                "name": s.name,
            }
            for s in cseries.load_series()
        ]
        return Response(
            body=f"# {len(rows)} component series (standards tables)\n"
            + render_agent_table(
                rows, schema=["series", "designation", "category", "sizes", "name"]
            )
            + "\n\nNext: get(kind='component', id='<series>', view='series') "
            "for a size table, or view='series' with q='M6x30 socket cap' "
            "to resolve a colloquial name."
        )

    def _render_one_series(self, series_id: str) -> Response:
        srs = cseries.find_series(series_id)
        if srs is None:
            known = ", ".join(s.series_id for s in cseries.load_series())
            raise NotFound(
                f"unknown series {series_id!r}; known: {known}",
                next="get(kind='component', view='series') for the registry",
            )
        spec_ids: list[str] = []
        for row in srs.sizes:
            for k in row.specs:
                if k not in spec_ids:
                    spec_ids.append(k)
        rows = []
        for row in srs.sizes:
            entry = {"size": row.key}
            entry.update({k: _fmt_spec(row.specs.get(k)) for k in spec_ids})
            if srs.length_spec is not None:
                entry["lengths"] = (
                    ", ".join(f"{x:g}" for x in row.lengths) if row.lengths else "—"
                )
            entry["stocked"] = row.stocking or "—"
            rows.append(entry)
        schema = ["size", *spec_ids]
        if srs.length_spec is not None:
            schema.append("lengths")
        # Availability last, so it reads as the tiebreak it is: pick on
        # fit, then prefer the size someone actually holds.
        schema.append("stocked")
        head = [f"# {srs.series_id} — {srs.name}"]
        if srs.designation:
            head.append(f"designation: {srs.designation}")
        head.append(f"category: {srs.category} · source: {srs.source}")
        if srs.specs:
            head.append(
                "every size: "
                + ", ".join(f"{k}={v}" for k, v in sorted(srs.specs.items()))
            )
        if srs.aliases:
            head.append("aka: " + ", ".join(srs.aliases))
        if any(row.stocking for row in srs.sizes):
            head.append(
                "`stocked` is a curated house judgement of how widely a size "
                "is held (universal | common | specialty), not a live number "
                "— get(id=<slug>, view='stock') asks a supplier"
            )
        tail = (
            f"\n\nNext: put(kind='component', series='{srs.series_id}', "
            f"size='{srs.sizes[0].key}"
            + (
                f"x{srs.sizes[0].lengths[0]:g}'"
                if srs.length_spec is not None and srs.sizes[0].lengths
                else "'"
            )
            + ") to mint one."
            if srs.sizes
            else ""
        )
        return Response(
            body="\n".join(head) + "\n" + render_agent_table(rows, schema=schema) + tail
        )

    def _render_series_resolve(self, q: str) -> Response:
        hits = cseries.resolve(q)
        if not hits:
            return Response(
                body=f"no series matches {q!r}\n\n"
                "Next: get(kind='component', view='series') to see the "
                "registry — it is standards families only (ISO fasteners, "
                "EN 10255 tube, cast acrylic sheet), not a supplier catalog."
            )
        rows = []
        for c in hits:
            designation = (
                c.size.key if c.length is None else f"{c.size.key}x{c.length:g}"
            )
            note = cseries.check_length(c.series, c.size, c.length)
            rows.append(
                {
                    "series": c.series.series_id,
                    "size": designation,
                    "name": c.series.name,
                    "matched": c.why,
                    "note": note or "",
                }
            )
        best = hits[0]
        best_size = (
            best.size.key if best.length is None else f"{best.size.key}x{best.length:g}"
        )
        return Response(
            body=f"# {len(rows)} candidate(s) for {q!r}\n"
            + render_agent_table(
                rows, schema=["series", "size", "name", "matched", "note"]
            )
            + "\n\nRanked, not picked — name the one you meant.\n"
            f"Next: put(kind='component', series='{best.series.series_id}', "
            f"size='{best_size}')"
        )

    def _put_made_of(self, slug: str, *, made_of: str, base: Response) -> Response:
        component_ref = self.store.get_ref(kind="component", id=slug)
        if component_ref is None:
            raise NotFound(
                f"component {slug!r} not found - create the entity first",
                next=(
                    f"put(kind='component', id={slug!r}, title='...', category='...')"
                ),
            )
        target = parse_link_target(made_of, store=self.store)
        if target.kind != "material":
            raise BadInput(
                f"made_of={made_of!r} resolves to kind={target.kind!r}; "
                "made_of= must resolve to a material ref",
                next="put(kind='component', id=<slug>, made_of='material:<slug>')",
            )
        self.store.component_link_made_of(
            component_ref_id=component_ref.id, material_ref_id=target.ref_id
        )
        material_ref = self.store.get_ref(kind="material", id=target.ref_id)
        material_name = material_ref.title if material_ref is not None else made_of
        return Response(body=base.body + f"\nmade-of: {material_name} ({made_of})")

    def _put_contains(
        self,
        slug: str,
        *,
        contains: str,
        qty: int | None,
        ref_designator: str | None,
        base: Response,
    ) -> Response:
        """Write/update/remove the ``contains`` (assembly-tree) edge from
        ``slug`` to ``contains=``. Mirrors ``_put_made_of``'s resolve-then-
        link shape, plus: the cycle guard, and the qty=0-removes /
        qty-omitted-preserves CRUD semantics
        (``component-assembly-tree`` (git-only))."""
        parent_ref = self.store.get_ref(kind="component", id=slug)
        if parent_ref is None:
            raise NotFound(
                f"component {slug!r} not found - create the entity first",
                next=(
                    f"put(kind='component', id={slug!r}, title='...', category='...')"
                ),
            )
        target = parse_link_target(contains, store=self.store)
        if target.kind != "component":
            raise BadInput(
                f"contains={contains!r} resolves to kind={target.kind!r}; "
                "contains= must resolve to a component ref",
                next=(
                    "put(kind='component', id=<slug>, "
                    "contains='component:<child slug>', qty=1)"
                ),
            )
        child_ref_id = target.ref_id
        child_ref = self.store.get_ref(kind="component", id=child_ref_id)
        child_label = f"{child_ref.title} ({contains})" if child_ref else contains

        qty_arg = _validate_qty(qty) if qty is not None else None

        if qty_arg == 0:
            removed = self.store.component_remove_contains(parent_ref.id, child_ref_id)
            note = (
                f"removed contains {slug} -> {child_label}"
                if removed
                else f"no such edge: {slug} -> {child_label}"
            )
            return Response(body=base.body + f"\n{note}")

        if self.store.component_would_cycle(parent_ref.id, child_ref_id):
            raise BadInput(
                f"contains={contains!r} would create a cycle - {child_label} "
                f"is {slug!r} itself or already one of its ancestors",
                next="the assembly tree must stay a DAG - pick a different "
                "child, or restructure the tree",
            )

        if qty_arg is None:
            existing_meta = self.store.component_contains_edge_meta(
                parent_ref.id, child_ref_id
            )
            qty_final = existing_meta.get("qty", 1) if existing_meta is not None else 1
        else:
            qty_final = qty_arg

        self.store.component_add_contains(
            parent_ref.id, child_ref_id, qty=qty_final, ref_designator=ref_designator
        )
        note = f"contains: {child_label} (qty={qty_final})"
        if ref_designator:
            note += f" [{ref_designator}]"
        return Response(body=base.body + f"\n{note}")

    _VE_KIND: ClassVar[str] = "component"
    _VE_ARG: ClassVar[str] = "spec"
    _VE_ID: ClassVar[str] = "spec_id"
    _VE_PLURAL: ClassVar[str] = "specs"
    _VE_METHODS: ClassVar[tuple[str, ...]] = _METHODS
    _VE_CREATE_HINT: ClassVar[str] = (
        "put(kind='component', id={slug}, title='...', category='...')"
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
    ) -> ComponentSpecRow:
        component_category = (entity_ref.meta or {}).get("category")
        spec_row = self.store.component_spec_get(key)
        if spec_row is None:
            spec_row = self._mint_spec(
                key,
                value=value,
                unit=unit,
                category_id=component_category,
                value_type=value_type,
                allowed_values=allowed_values,
            )
        else:
            self._check_type_consistency(
                spec_row, value_type=value_type, allowed_values=allowed_values
            )
        self._check_applicability(spec_row, component_category)
        return spec_row

    def _ve_insert(
        self, entity_ref_id: int, row: Mapping[str, Any], **kwargs: Any
    ) -> int:
        return self.store.component_value_insert(
            component_ref_id=entity_ref_id, spec_id=row["spec_id"], **kwargs
        )

    @staticmethod
    def _check_type_consistency(
        spec_row: ComponentSpecRow,
        *,
        value_type: str | None,
        allowed_values: list[Any] | None,
    ) -> None:
        """An explicit ``value_type=``/``allowed_values=`` against an
        *already-registered* spec must be consistent with it — this never
        re-mints, it only guards against a silently-conflicting
        declaration."""
        if value_type is not None and value_type != spec_row["value_type"]:
            raise BadInput(
                f"{spec_row['spec_id']} is already registered as "
                f"value_type={spec_row['value_type']!r} - "
                f"value_type={value_type!r} conflicts with the registered "
                "definition",
            )
        if allowed_values is not None:
            registered = spec_row.get("allowed_values") or []
            if set(allowed_values) != set(registered):
                raise BadInput(
                    f"{spec_row['spec_id']} is already registered with "
                    f"allowed_values={registered!r} - allowed_values="
                    f"{allowed_values!r} conflicts with the registered definition",
                )

    def _check_applicability(
        self, spec_row: ComponentSpecRow, component_category: str | None
    ) -> None:
        spec_category = spec_row.get("category_id")
        if spec_category is None:
            return  # universal
        if spec_category != component_category:
            raise BadInput(
                f"spec={spec_row['spec_id']!r} applies to category "
                f"{spec_category!r}, not {component_category!r}",
                next=(
                    "get(kind='component', view='specs') to see specs "
                    "applicable to this component's category"
                ),
            )

    def _mint_spec(
        self,
        spec_id: str,
        *,
        value: Any,
        unit: str | None,
        category_id: str | None,
        value_type: str | None = None,
        allowed_values: list[Any] | None = None,
    ) -> ComponentSpecRow:
        """Mint a fresh ``proposed`` spec when ``spec=`` is unknown, scoped
        to the *writing component's* category (never universal — see
        ``component-kind`` (git-only)'s resolved "Runtime spec-mint
        category" decision).

        With no explicit ``value_type=``, mirrors
        ``MaterialHandler._mint_property``'s value-shape inference. An
        explicit ``value_type=`` overrides inference — it's the only way
        to mint a ``categorical`` spec (which requires a non-empty
        ``allowed_values=``, and rejects ``unit=`` since categoricals are
        dimensionless)."""
        if value is None:
            raise NotFound(
                f"unknown spec {spec_id!r}",
                next=(
                    "get(kind='component', view='specs') to see the "
                    "registry, or mint a proposed spec by writing a "
                    f"value: put(kind='component', id=<slug>, "
                    f"spec={spec_id!r}, value=..., unit=<canonical unit "
                    "or omit for dimensionless>)"
                ),
            )
        canonical_unit = None if unit is None else str(unit).strip() or None
        name = spec_id.replace("_", " ").replace("-", " ").strip().title() or spec_id

        if value_type is not None:
            if value_type == "categorical":
                if not allowed_values:
                    raise BadInput(
                        f"minting {spec_id!r} as categorical requires a "
                        "non-empty allowed_values= list",
                        next=(
                            f"put(kind='component', id=<slug>, "
                            f"spec={spec_id!r}, value=..., "
                            "value_type='categorical', "
                            "allowed_values=['a', 'b'])"
                        ),
                    )
                if canonical_unit is not None:
                    raise BadInput(
                        f"cannot mint {spec_id!r} as categorical with "
                        f"unit={unit!r} - categorical specs are "
                        "dimensionless, drop unit=",
                    )
                dimension = "categorical"
                canonical_unit = None
            elif value_type in ("boolean", "text"):
                if canonical_unit is not None:
                    raise BadInput(
                        f"cannot mint {spec_id!r} as {value_type} with "
                        f"unit={unit!r} - {value_type} specs are "
                        "dimensionless, drop unit=",
                    )
                dimension = "dimensionless"
            else:  # quantity / ratio
                dimension = canonical_unit or "dimensionless"
            return self.store.component_spec_mint(
                spec_id=spec_id,
                name=name,
                canonical_unit=canonical_unit,
                dimension=dimension,
                value_type=value_type,
                category_id=category_id,
                allowed_values=allowed_values,
            )

        if isinstance(value, bool):
            if canonical_unit is not None:
                raise BadInput(
                    f"cannot mint {spec_id!r} as boolean with unit={unit!r} - "
                    "boolean specs are dimensionless, drop unit=",
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
        return self.store.component_spec_mint(
            spec_id=spec_id,
            name=name,
            canonical_unit=canonical_unit,
            dimension=dimension,
            value_type=inferred_type,
            category_id=category_id,
        )

    # ── get ──────────────────────────────────────────────────────────

    def get(
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
        spec: str | None = None,
        q: str | None = None,
        args: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Response:
        if view == "supplier-categories":
            return self._render_supplier_categories(id=id, q=q)
        if view == "series":
            return self._render_series(
                id=str(id).strip() if id is not None else None,
                q=str(q).strip() if q else None,
            )
        if view == "categories":
            return self._render_categories()
        if view == "specs" and id is None:
            return self._render_specs(category_id=None)
        if id is None:
            return self._list_components()
        slug = str(id).strip()
        ref = self.store.get_ref(kind="component", id=slug)
        if ref is None:
            raise NotFound(
                f"component {slug!r} not found",
                next="search(kind='component', q='...') or "
                "get(kind='component') to list every component",
            )
        if view == "specs":
            return self._render_specs(category_id=(ref.meta or {}).get("category"))
        if view == "tree":
            return self._render_tree(ref)
        if view == "bom":
            return self._render_bom(ref, spec=str(spec).strip() if spec else None)
        if view == "stock":
            return self._render_stock(ref, args=args)
        values = self.store.component_values_for_ref(ref.id)
        if view == "table":
            return self._render_table(ref, values)
        if view not in (None, "stored", "supplier-live"):
            raise BadInput(
                f"unknown view={view!r} for kind='component'", options=list(_VIEWS)
            )
        from precis.supply.live import live_lines

        if view == "supplier-live":
            return Response(body="\n".join(live_lines(ref.meta or {})), transient=True)
        response = self._render_page(ref, values)
        if view != "stored":
            response = replace(
                response,
                body=response.body + "\n" + "\n".join(live_lines(ref.meta or {})),
                transient=bool((ref.meta or {}).get("supplier_links")),
            )
        return response

    def _list_components(self) -> Response:
        refs = self.store.list_refs(kind="component", limit=50)
        if not refs:
            return Response(
                body="no components yet\n\n"
                "Next: put(kind='component', id='m6-a2-bolt', "
                "title='M6x20 A2 socket cap', category='fastener')"
            )
        rows = [
            {
                "id": r.slug or r.id,
                "name": r.title,
                "category": (r.meta or {}).get("category") or "—",
            }
            for r in refs
        ]
        return Response(
            body=f"# {len(rows)} component(s)\n"
            + render_agent_table(rows, schema=["id", "name", "category"])
        )

    def _render_categories(self) -> Response:
        cats = self.store.component_category_list()
        rows = [
            {
                "category_id": c["category_id"],
                "name": c["name"],
                "status": c["status"],
            }
            for c in cats
        ]
        noun = "category" if len(rows) == 1 else "categories"
        return Response(
            body=f"# {len(rows)} component {noun}\n"
            + render_agent_table(rows, schema=["category_id", "name", "status"])
        )

    def _render_supplier_categories(
        self, *, id: str | int | None, q: str | None
    ) -> Response:
        from precis.supply.base import SupplierError
        from precis.supply.digikey import DigiKeyAdapter

        category_id = None
        if id is not None:
            try:
                category_id = int(str(id))
                validate_filters(category_id, None)
            except ValueError as exc:
                raise BadInput(
                    "supplier category id must be a positive integer"
                ) from exc
        adapter = DigiKeyAdapter()
        why = adapter.configured()
        if why:
            return Response(body=f"Digi-Key categories unavailable: {why}")
        try:
            snapshot = adapter.categories(category_id)
            rows = []

            def visit(entries: list[dict[str, Any]], path: str = "") -> None:
                for entry in entries:
                    name = str(entry.get("Name") or "")
                    full = f"{path} > {name}" if path else name
                    if not q or q.casefold() in full.casefold():
                        rows.append(
                            {
                                "id": entry["CategoryId"],
                                "path": full,
                                "products": entry.get("ProductCount", 0),
                                "children": len(entry.get("Children") or []),
                            }
                        )
                    if q:
                        visit(entry.get("Children") or [], full)

            if category_id is not None:
                visit(snapshot.categories)
                if not q:
                    for entry in snapshot.categories:
                        visit(entry.get("Children") or [], str(entry.get("Name") or ""))
            else:
                visit(snapshot.categories)
            lines = [
                f"# Digi-Key categories · retrieved {snapshot.retrieved.isoformat()} · live, uncached, not stored",
                render_agent_table(
                    rows[:50], schema=["id", "path", "products", "children"]
                ),
            ]
            if len(rows) > 50:
                lines.append(
                    f"{len(rows) - 50} additional matches; narrow q= to a category path."
                )
            if category_id is not None:
                leaf = snapshot.categories and not snapshot.categories[0].get(
                    "Children"
                )
                if leaf:
                    for option in adapter.parameter_options(category_id):
                        name = str(option.get("ParameterName") or "")
                        values = option.get("FilterValues") or []
                        selected = [
                            v
                            for v in values
                            if not q
                            or q.casefold() in name.casefold()
                            or q.casefold() in str(v.get("ValueName", "")).casefold()
                        ]
                        if selected:
                            lines.append(
                                f"## {name} · parameter {option['ParameterId']} · {len(selected)} matching values"
                            )
                            lines.append(
                                render_agent_table(
                                    [
                                        {
                                            "value_id": v.get("ValueId"),
                                            "value": v.get("ValueName"),
                                            "products": v.get("ProductCount"),
                                        }
                                        for v in selected[:30]
                                    ],
                                    schema=["value_id", "value", "products"],
                                )
                            )
                            if len(selected) > 30:
                                lines.append(
                                    "More values omitted; narrow q= to a value label."
                                )
            lines.append(
                "Next: get(kind='component', view='supplier-categories', id=<category id>, q=<optional name/value>)"
            )
            lines.append(
                "Filter a part: get(kind='component', id=<component slug>, view='stock', args={'category_id': <id>, 'parameters': {'<parameter id>': ['<value id>']}})"
            )
            return Response(body="\n".join(lines), transient=True)
        except Exception as exc:
            error = str(exc) if isinstance(exc, SupplierError) else type(exc).__name__
            return Response(body=f"Digi-Key categories: {error}")

    def _render_specs(self, *, category_id: str | None) -> Response:
        specs = self.store.component_specs_list(category_id=category_id)
        rows = [
            {
                "spec_id": s["spec_id"],
                "name": s["name"],
                "unit": s["canonical_unit"] or "—",
                "category": s["category_id"] or "universal",
                "value_type": s["value_type"],
                "status": s["status"],
            }
            for s in specs
        ]
        noun = "spec" if len(rows) == 1 else "specs"
        return Response(
            body=f"# {len(rows)} component {noun}"
            + (f" applicable to category={category_id!r}" if category_id else "")
            + "\n"
            + render_agent_table(
                rows,
                schema=["spec_id", "name", "unit", "category", "value_type", "status"],
            )
        )

    # -- assembly tree views (view='tree' / 'bom') ----------------------

    def _render_tree(self, ref: Any) -> Response:
        label = f"{ref.title} ({ref.slug or ref.id})"
        lines = [f"# assembly tree: {label}"]
        if not self.store.component_contains_children(ref.id):
            lines.append("")
            lines.append("(leaf component — no contains children)")
            return Response(body="\n".join(lines))
        lines.append("")
        self._append_tree_lines(lines, ref.id, indent=0)
        return Response(body="\n".join(lines))

    def _append_tree_lines(self, lines: list[str], ref_id: int, *, indent: int) -> None:
        for child in self.store.component_contains_children(ref_id):
            child_ref = self.store.get_ref(kind="component", id=child["child_ref_id"])
            label = (
                f"{child_ref.title} ({child_ref.slug or child_ref.id})"
                if child_ref is not None
                else str(child["child_ref_id"])
            )
            bits = f"{'  ' * indent}- {label} x{child['qty']}"
            if child.get("ref"):
                bits += f" [{child['ref']}]"
            lines.append(bits)
            self._append_tree_lines(lines, child["child_ref_id"], indent=indent + 1)

    def _flatten_bom(
        self, ref_id: int, *, multiplier: int = 1
    ) -> list[tuple[int, int]]:
        """Recursively flatten the assembly tree rooted at ``ref_id`` to
        ``(leaf_ref_id, effective qty)`` pairs, multiplying qty down each
        path. A component with no ``contains`` children is a leaf by
        definition — the PCB-leaf boundary: a PCBA is one line
        item here, the rollup never descends into its internals."""
        children = self.store.component_contains_children(ref_id)
        if not children:
            return [(ref_id, multiplier)]
        pairs: list[tuple[int, int]] = []
        for child in children:
            pairs.extend(
                self._flatten_bom(
                    child["child_ref_id"], multiplier=multiplier * child["qty"]
                )
            )
        return pairs

    def _bom_leaves(self, ref_id: int) -> dict[int, int]:
        """``{leaf_ref_id: summed qty}`` — the flat BOM, one entry per
        distinct leaf even if it's reached via multiple paths."""
        summed: dict[int, int] = {}
        for leaf_id, qty in self._flatten_bom(ref_id):
            summed[leaf_id] = summed.get(leaf_id, 0) + qty
        return summed

    def _render_stock(
        self, ref: Any, *, args: dict[str, Any] | None = None
    ) -> Response:
        """``view='stock'`` — is this thing buyable, right now.

        Two answers, deliberately both shown (the owning backlog item's
        "Stock as a selection signal"): the **curated tier** from the
        series row, which is an offline house judgement that ranks M4×12
        above M14×55 with no network, and a **live supplier quote**, which
        is one distributor's warehouse at one moment. When no adapter is
        configured the view says which credential is missing rather than
        quietly showing the tier alone — a missing key and a part nobody
        stocks must not read the same.

        Known fasteners use series-derived thread/length/head-form words:
        distributors need descriptive keywords rather than ISO numbers.
        Hand-entered rows fall back to MPN when designation is absent.
        Each supplier reports success/empty/error separately; descriptions
        remain visible because keyword matches can be the wrong hardware.
        """
        meta = ref.meta or {}
        if args is not None and (
            not isinstance(args, dict) or set(args) - {"category_id", "parameters"}
        ):
            raise BadInput("stock args accept only category_id and parameters")
        filters = {
            key: value for key, value in (args or {}).items() if value is not None
        }
        try:
            validate_filters(filters.get("category_id"), filters.get("parameters"))
        except ValueError as exc:
            raise BadInput(str(exc)) from exc
        series_id = str(meta.get("series") or "")
        size = str(meta.get("size") or "")
        designation = str(meta.get("designation") or "")
        lines = [f"# stock — {ref.id}"]
        srs = cseries.find_series(series_id) if series_id else None
        row = srs.size(cseries.split_designation(size)[0]) if srs and size else None
        if srs is not None and row is not None and row.stocking:
            lines.append(
                f"availability tier: **{row.stocking}** — a curated house "
                f"judgement about {srs.series_id} {row.key}, not a live number; "
                "it is what ranks candidates when nothing can be asked."
            )
        else:
            lines.append(
                "availability tier: none recorded (hand-entered row, or a "
                "series without a judgement for this size)"
            )
        if srs is not None and row is not None and srs.category == "fastener":
            _, length = cseries.split_designation(size)
            thread = str(row.specs.get("thread_size") or row.key)
            family = srs.name.lower().split(",", 1)[0]
            if family.startswith("hexagon socket "):
                family = family.removeprefix("hexagon ")
            dimension = f"{thread} x {length:g}mm" if length is not None else thread
            query = f"{dimension} {family}"
            mapping = series_mapping(srs.series_id)
            if mapping and not filters:
                filters["category_id"] = mapping["category_id"]
                filters["criteria"] = {
                    "thread": thread,
                    "pitch": row.specs.get("thread_pitch"),
                    "length": length,
                    "length_parameter": "Length - Overall"
                    if srs.specs.get("head_form") in {"countersunk", "none"}
                    else "Length - Below Head",
                }
        elif not designation and meta.get("mpn"):
            query = str(meta["mpn"]).strip()
        else:
            query = " ".join(x for x in (designation or series_id, size) if x).strip()
        if not query:
            lines.append(
                "no designation or MPN on this row, so there is nothing to "
                "ask a supplier by — mint it from a series to get one."
            )
            return Response(body="\n".join(lines))
        why = supply.unavailable_reason()
        if why is not None:
            lines.append(f"live stock: unavailable — {why}")
            return Response(body="\n".join(lines))
        result = supply.quote(query, **filters)
        lines.append(f"live stock for {query!r}:")
        lines.extend(f"- {outcome.line()}" for outcome in result.outcomes)
        lines.extend(f"- {q.line()}" for q in result.quotes)
        return Response(body="\n".join(lines), transient=True)

    def _render_bom(self, ref: Any, *, spec: str | None) -> Response:
        spec_row: ComponentSpecRow | None = None
        if spec is not None:
            spec_row = self.store.component_spec_get(spec)
            if spec_row is None:
                raise BadInput(
                    f"unknown spec {spec!r}",
                    next="get(kind='component', view='specs') to see the registry",
                )

        leaves = self._bom_leaves(ref.id)
        n_leaves = len(leaves)
        leaf_noun = "leaf" if n_leaves == 1 else "leaves"

        schema = ["component", "qty", "unit_cost", "mass"]
        if spec_row is not None:
            schema.append(spec_row["spec_id"])

        rows: list[dict[str, Any]] = []
        total_cost = 0.0
        cost_covered = 0
        total_mass = 0.0
        mass_covered = 0
        spec_values: dict[int, str | None] = {}

        for leaf_id, qty in leaves.items():
            leaf_ref = self.store.get_ref(kind="component", id=leaf_id)
            leaf_slug = (leaf_ref.slug or leaf_ref.id) if leaf_ref else leaf_id
            leaf_name = leaf_ref.title if leaf_ref is not None else str(leaf_id)

            cost_val = self.store.component_current_spec_value(leaf_id, "unit_cost")
            cost_display = "—"
            if cost_val is not None and cost_val["value_num"] is not None:
                total_cost += qty * cost_val["value_num"]
                cost_covered += 1
                cost_display = _display_value(cost_val)

            mass_val = self.store.component_current_spec_value(leaf_id, "mass")
            mass_display = "—"
            if mass_val is not None and mass_val["value_num"] is not None:
                total_mass += qty * mass_val["value_num"]
                mass_covered += 1
                mass_display = _display_value(mass_val)

            row: dict[str, Any] = {
                "component": f"{leaf_name} ({leaf_slug})",
                "qty": qty,
                "unit_cost": cost_display,
                "mass": mass_display,
            }
            if spec_row is not None:
                v = self.store.component_current_spec_value(
                    leaf_id, spec_row["spec_id"]
                )
                display = _display_value(v) if v is not None else None
                spec_values[leaf_id] = display
                row[spec_row["spec_id"]] = display if display is not None else "—"
            rows.append(row)

        lines = [
            f"# BOM: {ref.title} ({ref.slug or ref.id}) — {n_leaves} {leaf_noun}",
            render_agent_table(rows, schema=schema),
            "",
            f"unit_cost total: {total_cost:g} — "
            f"unit_cost: {cost_covered} of {n_leaves} leaves",
            f"mass total: {total_mass:g} — mass: {mass_covered} of {n_leaves} leaves",
        ]

        if spec_row is not None:
            lines.append("")
            lines.append(_spec_uniformity_summary(spec_row["spec_id"], spec_values))

        return Response(body="\n".join(lines))

    def _render_table(
        self, ref: Any, values: list[ComponentValueRowWithSource]
    ) -> Response:
        if not values:
            return Response(
                body=f"# {ref.title} ({ref.slug}) — no recorded values yet\n\n"
                f"Next: put(kind='component', id={ref.slug!r}, "
                "spec='mass', value=0.01, unit='kg')"
            )
        rows = [
            {
                "spec": v["spec_id"],
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
                rows, schema=["spec", "value", "conditions", "maturity", "source"]
            )
        )

    def _render_page(
        self, ref: Any, values: list[ComponentValueRowWithSource]
    ) -> Response:
        meta = ref.meta or {}
        lines = [f"# component {ref.slug}: {ref.title}"]
        if meta.get("category"):
            lines.append(f"category: {meta['category']}")
        if meta.get("uom"):
            lines.append(f"uom: {meta['uom']}")
        if meta.get("mpn"):
            lines.append(f"mpn: {meta['mpn']}")
        if meta.get("manufacturer"):
            lines.append(f"manufacturer: {meta['manufacturer']}")
        if meta.get("sku"):
            lines.append(f"sku: {meta['sku']}")
        if meta.get("package"):
            lines.append(f"package: {meta['package']}")
        if meta.get("aliases"):
            lines.append("aka: " + ", ".join(meta["aliases"]))
        if meta.get("notes"):
            lines.append(f"notes: {meta['notes']}")
        from precis.supply.live import sources_lines

        lines.extend(sources_lines(meta))

        made_of_links = self.store.links_for(
            ref.id, direction="out", relation="made-of"
        )
        for link in made_of_links:
            material_ref = self.store.get_ref(kind="material", id=link.dst_ref_id)
            if material_ref is not None:
                lines.append(f"made-of: {material_ref.title} ({material_ref.slug})")

        if not values:
            lines += [
                "",
                "no recorded values yet",
                "",
                f"Next: put(kind='component', id={ref.slug!r}, "
                "spec='mass', value=0.01, unit='kg')",
            ]
            return Response(body="\n".join(lines))

        grouped: dict[str, list[ComponentValueRowWithSource]] = {}
        for v in values:
            grouped.setdefault(v["spec_id"], []).append(v)

        lines.append("")
        for spec_id, vs in grouped.items():
            spec_reg = self.store.component_spec_get(spec_id)
            unit = spec_reg.get("canonical_unit") if spec_reg else None
            spec_name = spec_reg.get("name") if spec_reg else None
            header = f"## {spec_name or spec_id} ({spec_id})"
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

    # ── link ─────────────────────────────────────────────────────────
    def link(
        self,
        *,
        id: str | int | None = None,
        target: str | None = None,
        mode: str = "add",
        rel: str | None = None,
        **_kw: Any,
    ) -> Response:
        """Add/remove a link from this component to another ref, e.g. the
        catalog part that realizes it (``rel='realized-by'``,
        ``target='part:C25804'``, which mints the part ref on first link).
        ``made-of`` and ``contains`` keep their own ``put`` doors (material
        check; qty and refdes on the edge)."""
        if rel in _PUT_DOOR_RELS:
            raise BadInput(
                f"rel={rel!r} is written through put, not link",
                next=_PUT_DOOR_RELS[rel],
            )
        if id is None or not str(id).strip():
            raise BadInput(
                "link(kind='component') requires id= (the component slug)",
                next="link(kind='component', id=<slug>, rel='realized-by', "
                "target='part:C25804')",
            )
        target = require_link_target("component", target)
        validate_link_mode(mode)
        ref = resolve_live_slug_ref(self.store, kind="component", id=str(id).strip())
        n_added, n_removed = apply_link_ops(
            self.store,
            ref.id,
            link=target if mode == "add" else None,
            unlink=target if mode == "remove" else None,
            rel=rel,
        )
        return Response(
            body=format_link_tag_ack(
                kind=self.spec.kind,
                ref_label=str(ref.slug),
                n_links_added=n_added,
                n_links_removed=n_removed,
                n_tags_added=0,
                n_tags_removed=0,
            )
        )

    def search(
        self,
        *,
        q: str | None = None,
        spec: str | None = None,
        category: str | None = None,
        min: float | None = None,
        max: float | None = None,
        maturity: str | None = None,
        page_size: int = 20,
        **_kw: Any,
    ) -> Response:
        if spec is not None:
            return self._search_values(
                spec=str(spec).strip(),
                min_val=min,
                max_val=max,
                maturity=maturity,
                category_id=category,
                limit=page_size,
            )
        if q is None or not str(q).strip():
            raise BadInput(
                "search(kind='component') requires q=, or the "
                "spec=/min=/max=/maturity= range filter",
                next="search(kind='component', q='M6') or "
                "search(kind='component', spec='max_working_pressure', min=20)",
            )
        return self._search_entities(str(q).strip(), limit=page_size)

    def _search_entities(self, q: str, *, limit: int) -> Response:
        hits = self.store.component_search_entities(q, limit=limit)
        if not hits:
            return Response(body=f"no component entries match {q!r}")
        rows = [
            {
                "id": slug or ref_id,
                "name": title,
                "category": (meta or {}).get("category") or "—",
            }
            for ref_id, slug, title, meta in hits
        ]
        return Response(
            body=f"# {len(rows)} component(s) matching {q!r}\n"
            + render_agent_table(rows, schema=["id", "name", "category"])
        )

    def _search_values(
        self,
        *,
        spec: str,
        min_val: float | None,
        max_val: float | None,
        maturity: str | None,
        category_id: str | None,
        limit: int,
    ) -> Response:
        if not spec:
            raise BadInput(
                "search(kind='component') spec= needs a non-empty spec_id",
                next="get(kind='component', view='specs')",
            )
        spec_row = self.store.component_spec_get(spec)
        if spec_row is None:
            raise NotFound(
                f"unknown spec {spec!r}",
                next="get(kind='component', view='specs')",
            )
        if (min_val is not None or max_val is not None) and spec_row[
            "value_type"
        ] not in ("quantity", "ratio"):
            raise BadInput(
                f"{spec!r} is a {spec_row['value_type']!r} spec - "
                "min=/max= apply only to numeric (quantity/ratio) specs",
                next=f"search(kind='component', spec={spec!r}) "
                "without min=/max=, or search(kind='component', q='...')",
            )
        if maturity is not None and maturity not in _MATURITIES:
            raise BadInput(f"maturity={maturity!r} must be one of {list(_MATURITIES)}")
        hits = self.store.component_search_values(
            spec_id=spec,
            min_val=min_val,
            max_val=max_val,
            maturity=maturity,
            category_id=category_id,
            limit=limit,
        )
        if not hits:
            return Response(
                body=f"no component values match spec={spec!r} "
                f"min={min_val!r} max={max_val!r} maturity={maturity!r} "
                f"category={category_id!r}"
            )
        unit = spec_row.get("canonical_unit") or ""
        rows = [
            {
                "component": v["component_title"],
                "value": _display_value(v) + (f" {unit}" if unit else ""),
                "conditions": _fmt_conditions(v["conditions"]),
                "maturity": v["maturity"],
                "source": _display_source(v),
            }
            for v in hits
        ]
        return Response(
            body=f"# {len(rows)} value(s) for {spec!r}"
            + (f" (unit={unit})" if unit else "")
            + "\n"
            + render_agent_table(
                rows, schema=["component", "value", "conditions", "maturity", "source"]
            )
        )


#: Relations a component writes through ``put``, with the call to use.
_PUT_DOOR_RELS: dict[str, str] = {
    "made-of": "put(kind='component', id=<slug>, made_of='material:<slug>')",
    "contains": "put(kind='component', id=<slug>, contains='component:<child>', qty=1)",
}


def _fmt_spec(value: Any) -> str:
    """One series-table cell. ``—`` for a spec this size doesn't carry (a
    hex nut has no head diameter) — an absence, never a zero."""
    if value is None:
        return "—"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{float(value):g}"
    return str(value)


def _validate_qty(qty: Any) -> int:
    """``qty=`` on a ``contains`` write must be a non-negative int (bools
    excluded — ``isinstance(True, int)`` is True in Python)."""
    if isinstance(qty, bool) or not isinstance(qty, int) or qty < 0:
        raise BadInput(
            f"qty={qty!r} must be a non-negative integer",
            next="put(kind='component', id=<slug>, contains=<child>, qty=1)",
        )
    return qty


def _spec_uniformity_summary(spec_id: str, spec_values: dict[int, str | None]) -> str:
    """The ``view='bom', spec=S`` consistency-query summary: uniform (every
    leaf carries the same current value), MIXED (distinct values + counts,
    naming any leaf with no recorded value), or "not recorded on any" — the
    latter is NEVER conflated with "uniform" (a spec no leaf carries is not
    vacuously consistent)."""
    n = len(spec_values)
    recorded = {k: v for k, v in spec_values.items() if v is not None}
    n_recorded = len(recorded)
    missing = n - n_recorded

    if n_recorded == 0:
        return f"{spec_id}: not recorded on any of {n} leaves"

    distinct = sorted(set(recorded.values()))
    if len(distinct) == 1 and missing == 0:
        return f"{spec_id}: all {distinct[0]!r} ({n_recorded}/{n} leaves)"

    counts: dict[str, int] = {}
    for v in recorded.values():
        counts[v] = counts.get(v, 0) + 1
    parts = [
        f"{v} ×{c}" for v, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    summary = f"{spec_id}: MIXED — " + ", ".join(parts)
    if missing:
        noun = "leaf" if missing == 1 else "leaves"
        verb = "has" if missing == 1 else "have"
        summary += f" ({missing} {noun} {verb} no {spec_id})"
    return summary


def _display_source(v: ComponentValueRowWithSource) -> str:
    ref_id = v.get("source_ref_id")
    kind = v.get("source_kind")
    if ref_id is not None and kind:
        handle = handle_registry.try_format(kind, ref_id) or f"{kind}:{ref_id}"
        chunk = v.get("source_chunk")
        return f"{handle}~{chunk}" if chunk else handle
    if v.get("source_url"):
        return str(v["source_url"])
    return "—"


__all__ = ["ComponentHandler"]
