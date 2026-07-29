"""ComponentHandler — a general procurable-part store: bolts, hoses, pipes,
beams, gaskets, bearings, adhesives, electronic components
(docs/proposals/component-kind.md).

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

``get`` renders the component page (specs grouped by spec, plus entity
facts and the made-of material), ``view='table'`` (tidy one-row-per-value),
``view='specs'`` (universal + this component's category specs), or
``view='categories'`` (the category registry). ``search`` matches
name/alias/mpn/manufacturer/category with ``q=``, or does the range-filter
read (``spec=/min=/max=/maturity=``, optionally narrowed by ``category=``).

See ``precis-component-help``.
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
_VIEWS: tuple[str, ...] = ("table", "specs", "categories")
_VALUE_TYPES: tuple[str, ...] = ("quantity", "ratio", "categorical", "boolean", "text")


class ComponentHandler(Handler):
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
            "it's made of. get(id=<slug>) is the component page; "
            "view='specs'/'categories' list the registries. "
            "search(spec=<spec_id>, min=, max=, maturity=, category=) is "
            "the range filter read; plain q= matches name/mpn/manufacturer/"
            "category. See precis-component-help."
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
            raise InitError("component: store required")
        self.store = hub.store

    def accepted_views(self, *, id: Any = None) -> list[str]:
        return list(_VIEWS)

    # ── put ──────────────────────────────────────────────────────────

    def put(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        spec: str | None = None,
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
        category: str | None = None,
        uom: str | None = None,
        made_of: str | None = None,
        meta: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Response:
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
                spec=str(spec).strip(),
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
        else:
            resp = self._put_entity(
                slug, title=title, category=category, uom=uom, meta=meta
            )

        if made_of is not None:
            resp = self._put_made_of(slug, made_of=str(made_of).strip(), base=resp)
        return resp

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

    def _resolve_category(self, category_id: str) -> dict[str, Any]:
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

    def _put_value(
        self,
        slug: str,
        *,
        spec: str,
        value: Any,
        unit: str | None,
        conditions: dict[str, Any] | None,
        maturity: str | None,
        source: str | None,
        chunk: str | None,
        as_of: str | None,
        value_type: str | None = None,
        allowed_values: list[Any] | None = None,
        value_low: float | None = None,
        value_high: float | None = None,
    ) -> Response:
        if not spec:
            raise BadInput(
                "put(kind='component') with spec= needs a non-empty spec_id",
                next="get(kind='component', view='specs') to see the registry",
            )
        component_ref = self.store.get_ref(kind="component", id=slug)
        if component_ref is None:
            raise NotFound(
                f"component {slug!r} not found - create the entity first",
                next=(
                    f"put(kind='component', id={slug!r}, title='...', category='...')"
                ),
            )
        if conditions is not None and not isinstance(conditions, dict):
            raise BadInput("put(kind='component') conditions= must be a dict")

        self._validate_type_args(value_type, allowed_values)

        component_category = (component_ref.meta or {}).get("category")

        spec_row = self.store.component_spec_get(spec)
        if spec_row is None:
            spec_row = self._mint_spec(
                spec,
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
        self._check_unit(spec_row, unit)
        value_kwargs = self._route_value(
            spec_row, value, value_low=value_low, value_high=value_high
        )

        if maturity is not None and maturity not in _MATURITIES:
            raise BadInput(
                f"maturity={maturity!r} must be one of {list(_MATURITIES)}",
                next=f"put(kind='component', id={slug!r}, spec={spec!r}, "
                f"value=..., maturity='lab')",
            )

        source_ref_id, source_chunk, source_url = self._resolve_source(source, chunk)

        value_id = self.store.component_value_insert(
            component_ref_id=component_ref.id,
            spec_id=spec_row["spec_id"],
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
                f"recorded {slug}.{spec_row['spec_id']} = "
                f"{display_value}{unit_note} "
                f"(id={value_id}, maturity={maturity or 'lab'})"
                f"{source_note}"
            )
        )

    @staticmethod
    def _validate_type_args(
        value_type: str | None, allowed_values: list[Any] | None
    ) -> None:
        """Validate ``value_type=``/``allowed_values=`` shape, independent
        of whether this write mints a fresh spec or targets an existing
        one (``_check_type_consistency`` covers the latter)."""
        if value_type is not None and value_type not in _VALUE_TYPES:
            raise BadInput(
                f"value_type={value_type!r} must be one of {list(_VALUE_TYPES)}",
            )
        if allowed_values is not None and value_type != "categorical":
            raise BadInput(
                "allowed_values= is only valid with value_type='categorical'",
                next=(
                    "put(kind='component', id=<slug>, spec=<spec_id>, "
                    "value=..., value_type='categorical', "
                    "allowed_values=['a', 'b'])"
                ),
            )

    @staticmethod
    def _check_type_consistency(
        spec_row: dict[str, Any],
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
        self, spec_row: dict[str, Any], component_category: str | None
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
    ) -> dict[str, Any]:
        """Mint a fresh ``proposed`` spec when ``spec=`` is unknown, scoped
        to the *writing component's* category (never universal — see
        ``docs/proposals/component-kind.md``'s resolved "Runtime spec-mint
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

    @staticmethod
    def _check_unit(spec_row: dict[str, Any], unit: str | None) -> None:
        canonical = spec_row.get("canonical_unit")
        given = None if unit is None else (str(unit).strip() or None)
        if canonical is not None:
            if given != canonical:
                raise BadInput(
                    f"unit={unit!r} is not {spec_row['spec_id']}'s canonical "
                    f"unit ({canonical!r}) - v1 is canonical-unit-only, "
                    "no conversion",
                    next=(
                        f"put(kind='component', id=<slug>, "
                        f"spec={spec_row['spec_id']!r}, value=..., "
                        f"unit={canonical!r})"
                    ),
                )
        elif given is not None:
            raise BadInput(
                f"{spec_row['spec_id']} has no canonical unit "
                "(dimensionless/categorical/boolean/text) - drop unit=",
                next=(
                    f"put(kind='component', id=<slug>, "
                    f"spec={spec_row['spec_id']!r}, value=...)"
                ),
            )

    @staticmethod
    def _route_value(
        spec_row: dict[str, Any],
        value: Any,
        *,
        value_low: float | None = None,
        value_high: float | None = None,
    ) -> dict[str, Any]:
        spec_id = spec_row["spec_id"]
        value_type = spec_row["value_type"]
        has_band = value_low is not None or value_high is not None

        if has_band and value_type not in ("quantity", "ratio"):
            raise BadInput(
                f"{spec_id} is a {value_type} spec - value_low=/value_high= "
                "apply only to numeric (quantity/ratio) specs",
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
                        f"{spec_id} is a {value_type} spec - value= must be "
                        f"numeric, got {value!r}"
                    )
                try:
                    num = float(value)
                except (TypeError, ValueError):
                    raise BadInput(
                        f"{spec_id} is a {value_type} spec - value= must be "
                        f"numeric, got {value!r}"
                    ) from None
            elif value_low is not None and value_high is not None:
                num = (float(value_low) + float(value_high)) / 2
            elif has_band:
                raise BadInput(
                    f"put(kind='component', spec={spec_id!r}) needs "
                    "value=, or both value_low= and value_high=",
                )
            else:
                raise BadInput(
                    f"put(kind='component', spec={spec_id!r}) needs value=",
                )
            out: dict[str, Any] = {"value_num": num}
            if value_low is not None:
                out["value_low"] = float(value_low)
            if value_high is not None:
                out["value_high"] = float(value_high)
            return out
        if value is None:
            raise BadInput(
                f"put(kind='component', spec={spec_id!r}) needs value=",
            )
        if value_type == "boolean":
            b = _coerce_bool(value)
            if b is None:
                raise BadInput(
                    f"{spec_id} is boolean - value= must be true/false, got {value!r}"
                )
            return {"value_bool": b}
        if value_type == "categorical":
            s = str(value).strip()
            allowed = spec_row.get("allowed_values") or []
            if allowed and s not in allowed:
                raise BadInput(
                    f"{spec_id} value {s!r} is not in allowed_values {allowed!r}",
                    next=f"pick one of {allowed!r}",
                )
            return {"value_text": s}
        # text
        return {"value_text": str(value).strip()}

    def _resolve_source(
        self, source: str | None, chunk: str | None
    ) -> tuple[int | None, str | None, str | None]:
        """Resolve ``source=``/``chunk=`` to ``(source_ref_id, source_chunk,
        source_url)``. Copied from ``MaterialHandler._resolve_source`` — see
        that docstring for the full contract."""
        if source is None or not str(source).strip():
            if chunk is not None:
                raise BadInput(
                    "chunk= requires source= (the ref the chunk belongs to)",
                    next="put(kind='component', id=<slug>, spec=..., "
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
                f"component sources must be one of {list(_SOURCE_KINDS)}, or "
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
        values = self.store.component_values_for_ref(ref.id)
        if view == "table":
            return self._render_table(ref, values)
        if view is not None:
            raise BadInput(
                f"unknown view={view!r} for kind='component'",
                options=list(_VIEWS),
                next="omit view= for the component page",
            )
        return self._render_page(ref, values)

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

    def _render_table(self, ref: Any, values: list[dict[str, Any]]) -> Response:
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

    def _render_page(self, ref: Any, values: list[dict[str, Any]]) -> Response:
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

        grouped: dict[str, list[dict[str, Any]]] = {}
        for v in values:
            grouped.setdefault(v["spec_id"], []).append(v)

        lines.append("")
        for spec_id, vs in grouped.items():
            spec_row = self.store.component_spec_get(spec_id) or {}
            unit = spec_row.get("canonical_unit")
            header = f"## {spec_row.get('name') or spec_id} ({spec_id})"
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


__all__ = ["ComponentHandler"]
