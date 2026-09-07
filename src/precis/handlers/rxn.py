"""RxnHandler — the sourced reaction-fact store (``rxn`` kind).

Design-of-record: ``docs/backlog/reaction-kind-and-synthesis-cost.md``.

The point of this kind is one property, inherited from ``material``: **many
rows per (reaction, property) is the feature.** Twelve reported yields for one
transformation across twelve papers IS the answer; nothing here collapses them
into an average, and the read shows the spread with each value's citation.

Two writes share one ``put``, discriminated by whether ``property=`` is
present:

* ``put(kind='rxn', id=<slug>, rxn_smiles='A.B>>C', reaction_class='RXNO:...')``
  — upsert the reaction **entity**. The reaction SMILES is canonicalised and
  both identity keys are derived (:mod:`precis.handlers._rxn_ids`).
* ``put(kind='rxn', id=<slug>, property='yield', value=83, unit='%',
  conditions={...}, source='paper:<slug>', chunk='pc123', method='measured')``
  — append a sourced **value** row. The entity must already exist.

``get`` renders the reaction page grouped by property (every reported value
with its conditions and citation), ``view='properties'`` the registry.
``search(property=..., min=, max=, reaction_class=)`` is the precedent read
this kind exists for: *"what yields do amide couplings actually give?"*

NOT in this slice: scoring, route integration, bulk import. See the ship order
in the design doc.

Duplication note: the registry/value machinery is deliberately the ``material``
shape. Factoring the three copies (material, component, rxn) onto a shared core
is tracked in ``docs/backlog/material-component-shared-core.md`` — doing it
here would mean editing two shipped kinds to land a third.

See ``precis-rxn-help``.
"""

from __future__ import annotations

from typing import Any, ClassVar

from precis.dispatch import Hub, InitError
from precis.errors import BadInput, NotFound
from precis.format import render_agent_table
from precis.handlers._rxn_ids import (
    RxnParseError,
    parse_reaction_smiles,
    rdkit_available,
)
from precis.protocol import Handler, KindSpec
from precis.response import Response

_MATURITIES: tuple[str, ...] = ("commercial", "lab", "speculative")

#: ``rxn_values.method`` — HOW the value was obtained. Wider than material's
#: vocabulary because a reaction store ingests bulk-extracted data: the whole
#: point of the axis is that a hand-curated measured yield must be
#: distinguishable from one text-mined out of a patent or predicted by a model.
_METHODS: tuple[str, ...] = (
    "measured",
    "patent-extracted",
    "literature-reported",
    "predicted",
    "estimated",
    "computed",
)

#: Kinds that may ground a value row. ``patent`` is included where material
#: omits it — patents are where the reaction literature actually lives.
_SOURCE_KINDS: tuple[str, ...] = ("paper", "patent", "datasheet")

_VIEWS: tuple[str, ...] = ("table", "properties")
_VALUE_TYPES: tuple[str, ...] = ("quantity", "ratio", "categorical", "boolean", "text")


def _display_value(v: dict[str, Any]) -> str:
    if v.get("value_num") is not None:
        base = f"{v['value_num']:g}"
        low, high = v.get("value_low"), v.get("value_high")
        if low is not None and high is not None:
            return f"{base} ({low:g}–{high:g})"
        return base
    if v.get("value_bool") is not None:
        return str(v["value_bool"])
    if v.get("value_text") is not None:
        return str(v["value_text"])
    return "—"


def fmt_smiles(smiles: Any) -> str:
    """Wrap a SMILES in a code span before it reaches a markdown renderer.

    Not cosmetic. A stereocentre followed by a branch — ``C[C@H](N)C(=O)O``,
    i.e. alanine, and a large share of drug-like molecules — matches GFM's
    inline-link grammar ``[text](target)`` and renders as a **link** with text
    ``C@H`` pointing at ``N``. The SMILES silently disappears from the output.
    (The same grammar collision reddened this repo's own doc-link test on a
    pentavalent-nitrogen example.) ``*`` and ``_`` in other SMILES would
    likewise be read as emphasis.

    A code span neutralises all of it, and is safe unconditionally: SMILES
    have no backtick in their grammar, so there is nothing to escape.

    Only for **display**. Never wrap on the way into the store or into rdkit —
    what we persist stays the bare canonical string.
    """
    s = str(smiles).strip()
    return f"`{s}`" if s else "—"


def _fmt_conditions(conditions: Any) -> str:
    """``k=`v``` pairs, values in code spans.

    Values are source-supplied and routinely chemistry: the ligand notation
    ``Pd[P(t-Bu)3](OAc)2`` matches markdown's inline-link grammar exactly as a
    SMILES stereocentre does, and would render as a link, dropping the catalyst
    from the page. Keys are ours (plain identifiers) and stay bare.
    """
    if not isinstance(conditions, dict) or not conditions:
        return ""
    return ", ".join(f"{k}=`{v}`" for k, v in sorted(conditions.items()))


class RxnHandler(Handler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="rxn",
        title="Reaction",
        description=(
            "A sourced reaction-fact store — a transformation (reaction "
            "SMILES) plus per-property sourced values. "
            "put(id=<slug>, rxn_smiles='A.B>>C', reaction_class='RXNO:0000024') "
            "upserts the entity (canonicalises the SMILES, derives both "
            "identity keys); put(id=<slug>, property='yield', value=83, "
            "unit='%', conditions={...}, method='measured', "
            "source='paper:<slug>', chunk='pc123') appends a sourced value. "
            "MANY rows per (reaction, property) is intended — the spread "
            "across sources IS the answer, never an average. get(id=<slug>) "
            "is the reaction page grouped by property; view='properties' "
            "lists the registry. search(property=, min=, max=, "
            "reaction_class=) is the precedent read; plain q= matches "
            "title/SMILES/class. See precis-rxn-help."
        ),
        supports_get=True,
        supports_put=True,
        supports_search=True,
        is_numeric=False,
        id_required=False,
        placement="artifact",
        corpus_role="none",
        views=_VIEWS,
    )

    def __init__(self, *, hub: Hub) -> None:
        if hub.store is None:
            raise InitError("rxn: store required")
        self.store = hub.store

    # ── put ──────────────────────────────────────────────────────────

    def put(
        self,
        *,
        id: str | int | None = None,
        rxn_smiles: str | None = None,
        reaction_class: str | None = None,
        property: str | None = None,
        value: Any = None,
        unit: str | None = None,
        conditions: dict[str, Any] | None = None,
        maturity: str | None = None,
        method: str | None = None,
        source_licence: str | None = None,
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
                "put(kind='rxn') requires id=<slug>",
                next=(
                    "put(kind='rxn', id='fischer-etoac', "
                    "title='Fischer esterification -> ethyl acetate', "
                    "rxn_smiles='CC(=O)O.OCC>>CC(=O)OCC.O')"
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
                method=method,
                source_licence=source_licence,
                source=source,
                chunk=chunk,
                as_of=as_of,
                value_type=value_type,
                allowed_values=allowed_values,
                value_low=value_low,
                value_high=value_high,
            )
        return self._put_entity(
            slug,
            title=title,
            rxn_smiles=rxn_smiles,
            reaction_class=reaction_class,
            meta=meta,
        )

    def _put_entity(
        self,
        slug: str,
        *,
        title: str | None,
        rxn_smiles: str | None,
        reaction_class: str | None,
        meta: dict[str, Any] | None,
    ) -> Response:
        if meta is not None and not isinstance(meta, dict):
            raise BadInput(
                "put(kind='rxn') meta= must be a dict",
                next="put(kind='rxn', id=..., meta={'notes': '...'})",
            )
        existing = self.store.get_ref(kind="rxn", id=slug)
        if existing is None and rxn_smiles is None:
            raise BadInput(
                f"creating rxn {slug!r} requires rxn_smiles=",
                next=(
                    f"put(kind='rxn', id={slug!r}, "
                    "rxn_smiles='CC(=O)O.OCC>>CC(=O)OCC.O')"
                ),
            )

        patch: dict[str, Any] = dict(meta or {})
        if reaction_class is not None:
            patch["reaction_class"] = str(reaction_class).strip()
        notes: list[str] = []

        if rxn_smiles is not None:
            raw = str(rxn_smiles).strip()
            patch["rxn_smiles_raw"] = raw
            if rdkit_available():
                try:
                    ident = parse_reaction_smiles(raw)
                except RxnParseError as exc:
                    raise BadInput(
                        f"rxn_smiles is not a usable reaction SMILES: {exc}",
                        next=(
                            "expected 'reactants>>products' or "
                            "'reactants>agents>products', each a '.'-joined "
                            "list of parseable SMILES"
                        ),
                    ) from exc
                patch["rxn_smiles"] = ident.canonical_smiles
                patch["uid_strict"] = ident.uid_strict
                patch["uid_transform"] = ident.uid_transform
                patch["desired_product"] = ident.desired_product
                # Surface a same-chemistry sibling rather than silently
                # creating a duplicate: the transform key is what converges
                # sources that record byproducts differently.
                for ref_id, other_slug, other_title, _m in self.store.rxn_find_by_uid(
                    ident.uid_transform, axis="transform"
                ):
                    if other_slug != slug:
                        notes.append(
                            f"note: same transformation already recorded as "
                            f"{other_slug or ref_id} ({other_title}) — "
                            f"consider adding values there instead"
                        )
            else:
                # Degrade honestly: store the string, do not invent a key.
                notes.append(
                    "note: rdkit ([chem] extra) not installed — SMILES stored "
                    "verbatim, identity keys NOT derived; precedent lookup by "
                    "transformation will not find this row"
                )

        ttl = (title or (existing.title if existing is not None else slug)).strip()
        ref, created = self.store.rxn_entity_upsert(
            slug=slug, title=ttl or slug, meta_patch=patch
        )
        rmeta = ref.meta or {}
        lines = [f"{'created' if created else 'updated'} rxn {slug} ({ref.title})"]
        if rmeta.get("rxn_smiles"):
            lines.append(f"smiles: {fmt_smiles(rmeta['rxn_smiles'])}")
        if rmeta.get("reaction_class"):
            lines.append(f"class: {rmeta['reaction_class']}")
        if rmeta.get("uid_transform"):
            lines.append(
                f"uid: transform={rmeta['uid_transform']} "
                f"strict={rmeta.get('uid_strict', '?')}"
            )
        lines.extend(notes)
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
        method: str | None,
        source_licence: str | None,
        source: str | None,
        chunk: str | None,
        as_of: str | None,
        value_type: str | None,
        allowed_values: list[Any] | None,
        value_low: float | None,
        value_high: float | None,
    ) -> Response:
        if not property:
            raise BadInput(
                "put(kind='rxn') with property= needs a non-empty prop_id",
                next="get(kind='rxn', view='properties') to see the registry",
            )
        rxn_ref = self.store.get_ref(kind="rxn", id=slug)
        if rxn_ref is None:
            raise NotFound(
                f"rxn {slug!r} not found - create the entity first",
                next=(
                    f"put(kind='rxn', id={slug!r}, "
                    "rxn_smiles='CC(=O)O.OCC>>CC(=O)OCC.O')"
                ),
            )
        if conditions is not None and not isinstance(conditions, dict):
            raise BadInput(
                "put(kind='rxn') conditions= must be a dict",
                next=(
                    "conditions={'solvent': 'toluene', 'catalyst': 'H2SO4', "
                    "'yield_type': 'isolated'}"
                ),
            )
        if value_type is not None and value_type not in _VALUE_TYPES:
            raise BadInput(
                f"value_type={value_type!r} must be one of {list(_VALUE_TYPES)}"
            )
        if allowed_values is not None and value_type != "categorical":
            raise BadInput(
                "allowed_values= is only valid with value_type='categorical'"
            )

        prop = self.store.rxn_property_get(property)
        if prop is None:
            prop = self._mint_property(
                property,
                value=value,
                unit=unit,
                value_type=value_type,
                allowed_values=allowed_values,
            )
        elif value_type is not None and value_type != prop["value_type"]:
            raise BadInput(
                f"{prop['prop_id']} is already registered as "
                f"value_type={prop['value_type']!r} - value_type={value_type!r} "
                "conflicts with the registered definition"
            )

        self._check_unit(prop, unit)
        value_kwargs = self._route_value(
            prop, value, value_low=value_low, value_high=value_high
        )

        if maturity is not None and maturity not in _MATURITIES:
            raise BadInput(
                f"maturity={maturity!r} must be one of {list(_MATURITIES)}",
                next=f"put(kind='rxn', id={slug!r}, property={property!r}, "
                "value=..., maturity='lab')",
            )
        if method is not None and method not in _METHODS:
            raise BadInput(
                f"method={method!r} must be one of {list(_METHODS)}",
                next=f"put(kind='rxn', id={slug!r}, property={property!r}, "
                "value=..., method='measured')",
            )

        source_ref_id, source_chunk, source_url = self._resolve_source(source, chunk)

        value_id = self.store.rxn_value_insert(
            rxn_ref_id=rxn_ref.id,
            property_id=prop["prop_id"],
            conditions=conditions,
            maturity=maturity or "lab",
            method=method,
            source_licence=source_licence,
            source_ref_id=source_ref_id,
            source_chunk=source_chunk,
            source_url=source_url,
            as_of=as_of,
            **value_kwargs,
        )
        shown = _display_value(value_kwargs)
        unit_note = f" {prop['canonical_unit']}" if prop.get("canonical_unit") else ""
        src = ""
        if source_ref_id is not None:
            src = f" (source={source!r})"
        elif source_url is not None:
            src = f" (source_url={source_url!r})"
        return Response(
            body=(
                f"recorded {slug}.{prop['prop_id']} = {shown}{unit_note} "
                f"(id={value_id}, maturity={maturity or 'lab'}"
                f"{', method=' + method if method else ''}){src}"
            )
        )

    # ── put helpers ──────────────────────────────────────────────────

    def _mint_property(
        self,
        prop_id: str,
        *,
        value: Any,
        unit: str | None,
        value_type: str | None,
        allowed_values: list[Any] | None,
    ) -> dict[str, Any]:
        """Mint a fresh ``proposed`` property for an unknown ``property=``.

        Never mints ``core`` — that tier is curated by migration only.
        """
        if value is None:
            raise NotFound(
                f"unknown property {prop_id!r}",
                next=(
                    "get(kind='rxn', view='properties') to see the registry, "
                    f"or mint a proposed one by writing a value: "
                    f"put(kind='rxn', id=<slug>, property={prop_id!r}, "
                    "value=..., unit=<canonical unit or omit>)"
                ),
            )
        canonical_unit = None if unit is None else (str(unit).strip() or None)
        name = prop_id.replace("_", " ").replace("-", " ").strip().title() or prop_id
        vt = value_type
        if vt is None:
            if isinstance(value, bool):
                vt = "boolean"
            elif isinstance(value, (int, float)):
                vt = "quantity"
            else:
                vt = "text"
        if vt == "categorical" and not allowed_values:
            raise BadInput(
                f"minting {prop_id!r} as categorical requires a non-empty "
                "allowed_values= list"
            )
        if vt in ("categorical", "boolean", "text") and canonical_unit is not None:
            raise BadInput(
                f"cannot mint {prop_id!r} as {vt} with unit={unit!r} - "
                f"{vt} properties are dimensionless, drop unit="
            )
        dimension = (
            "categorical"
            if vt == "categorical"
            else (canonical_unit or "dimensionless")
        )
        return self.store.rxn_property_mint(
            prop_id=prop_id,
            name=name,
            canonical_unit=canonical_unit,
            dimension=dimension,
            value_type=vt,
            allowed_values=allowed_values,
            description=None,
        )

    @staticmethod
    def _check_unit(prop: dict[str, Any], unit: str | None) -> None:
        """v1 is canonical-unit-only: a non-canonical unit is rejected, naming
        the canonical one. No conversion (same contract as ``material``)."""
        canonical = prop.get("canonical_unit")
        if unit is None:
            return
        given = str(unit).strip()
        if canonical is None:
            raise BadInput(
                f"{prop['prop_id']} is dimensionless - drop unit= (got {given!r})"
            )
        if given != canonical:
            raise BadInput(
                f"{prop['prop_id']} is stored in {canonical!r}, got {given!r} - "
                "v1 does no unit conversion",
                next=f"convert to {canonical!r} before writing",
            )

    @staticmethod
    def _route_value(
        prop: dict[str, Any],
        value: Any,
        *,
        value_low: float | None,
        value_high: float | None,
    ) -> dict[str, Any]:
        """Route ``value=`` into the right typed column for the property."""
        vt = prop["value_type"]
        prop_id = prop["prop_id"]
        if value is None:
            raise BadInput(f"put(kind='rxn') property={prop_id!r} requires value=")
        if vt == "boolean":
            if not isinstance(value, bool):
                raise BadInput(f"{prop_id} is boolean - value= must be true/false")
            return {"value_bool": value}
        if vt in ("categorical", "text"):
            text = str(value)
            allowed = prop.get("allowed_values")
            if vt == "categorical" and allowed and text not in allowed:
                raise BadInput(
                    f"{prop_id} value {text!r} is not in allowed_values={allowed!r}"
                )
            return {"value_text": text}
        # quantity / ratio
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            try:
                num = float(value)
            except (TypeError, ValueError) as exc:
                raise BadInput(
                    f"{prop_id} is {vt} - value= must be numeric, got {value!r}"
                ) from exc
        else:
            num = float(value)
        out: dict[str, Any] = {"value_num": num}
        if (value_low is None) != (value_high is None):
            raise BadInput(
                "value_low= and value_high= must be given together (a band), "
                "or neither (a point value)"
            )
        if value_low is not None and value_high is not None:
            if value_low > value_high:
                raise BadInput(
                    f"value_low={value_low} is above value_high={value_high}"
                )
            out["value_low"] = float(value_low)
            out["value_high"] = float(value_high)
        return out

    def _resolve_source(
        self, source: str | None, chunk: str | None
    ) -> tuple[int | None, str | None, str | None]:
        """Resolve ``source=``/``chunk=`` to
        ``(source_ref_id, source_chunk, source_url)``."""
        if source is None or not str(source).strip():
            if chunk is not None:
                raise BadInput(
                    "chunk= requires source= (the ref the chunk belongs to)",
                    next=(
                        "put(kind='rxn', id=<slug>, property='yield', "
                        "value=83, source='paper:<slug>', chunk='pc123')"
                    ),
                )
            return None, None, None
        s = str(source).strip()
        if s.lower().startswith(("http://", "https://")):
            if chunk is not None:
                raise BadInput(
                    "chunk= is only meaningful with a ref source=, not a URL"
                )
            return None, None, s
        if ":" not in s:
            raise BadInput(
                f"source={s!r} must be '<kind>:<slug>' or a URL",
                next="source='paper:my-paper-slug'",
            )
        kind, _, ident = s.partition(":")
        kind = kind.strip()
        if kind not in _SOURCE_KINDS:
            raise BadInput(
                f"source kind {kind!r} cannot ground a reaction value - "
                f"one of {list(_SOURCE_KINDS)}",
                next="source='paper:<slug>' | 'patent:<slug>' | 'datasheet:<slug>'",
            )
        ref = self.store.get_ref(kind=kind, id=ident.strip())
        if ref is None:
            raise NotFound(f"source {s!r} not found")
        return ref.id, (str(chunk).strip() if chunk is not None else None), None

    # ── get ──────────────────────────────────────────────────────────

    def get(
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
        **_kw: Any,
    ) -> Response:
        v = (view or "").strip().lower()
        if v in ("properties", "registry"):
            return self._render_registry()
        if v and v not in ("table", "page"):
            raise BadInput(
                f"unknown rxn view {view!r}",
                next="view='properties' (the registry) | omit for the page",
            )
        if id is None or (isinstance(id, str) and id.strip() in ("", "/")):
            return self._render_list()
        slug = str(id).strip()
        ref = self.store.get_ref(kind="rxn", id=slug)
        if ref is None:
            raise NotFound(f"rxn {slug!r} not found")
        return self._render_page(ref)

    def _render_registry(self) -> Response:
        rows = self.store.rxn_properties_list()
        if not rows:
            return Response(body="no rxn properties registered")
        table = [
            {
                "property": r["prop_id"],
                "unit": r["canonical_unit"] or "—",
                "type": r["value_type"],
                "tier": r["status"],
                "description": (r["description"] or "")[:70],
            }
            for r in rows
        ]
        return Response(
            body=f"# rxn properties ({len(rows)})\n"
            + render_agent_table(
                table, schema=["property", "unit", "type", "tier", "description"]
            )
        )

    def _render_list(self) -> Response:
        refs = self.store.list_refs(kind="rxn", order_by="id_desc", limit=50)
        if not refs:
            return Response(
                body="no reactions yet\n\nNext: put(kind='rxn', "
                "id='fischer-etoac', rxn_smiles='CC(=O)O.OCC>>CC(=O)OCC.O')"
            )
        rows = []
        for r in refs:
            m = r.meta or {}
            rows.append(
                {
                    "rxn": r.slug or r.id,
                    "title": r.title,
                    "smiles": fmt_smiles(
                        m.get("rxn_smiles") or m.get("rxn_smiles_raw") or ""
                    ),
                    "class": m.get("reaction_class") or "—",
                }
            )
        return Response(
            body=f"# {len(refs)} reaction(s)\n"
            + render_agent_table(rows, schema=["rxn", "title", "smiles", "class"])
        )

    def _render_page(self, ref: Any) -> Response:
        """The reaction page: every reported value, grouped by property, with
        its conditions and citation. **Never averaged** — the spread is the
        finding, and collapsing it would be the lie this schema prevents."""
        meta = ref.meta or {}
        head = [f"# rxn {ref.slug or ref.id} — {ref.title}"]
        if meta.get("rxn_smiles") or meta.get("rxn_smiles_raw"):
            head.append(
                f"smiles: {fmt_smiles(meta.get('rxn_smiles') or meta['rxn_smiles_raw'])}"
            )
        if meta.get("reaction_class"):
            head.append(f"class: {meta['reaction_class']}")
        if meta.get("uid_transform"):
            head.append(
                f"uid: transform={meta['uid_transform']} "
                f"strict={meta.get('uid_strict', '?')}"
            )
        if not meta.get("uid_transform"):
            head.append(
                "⚠ no identity keys — rdkit was unavailable at write time; "
                "this reaction will not be found by transformation lookup"
            )

        values = self.store.rxn_values_for_ref(ref.id)
        if not values:
            return Response(
                body="\n".join(head) + "\n\n(no values yet)\n\nNext: put(kind='rxn', "
                f"id={ref.slug or ref.id!r}, property='yield', value=83, "
                "unit='%', source='paper:<slug>', chunk='pc123')"
            )

        by_prop: dict[str, list[dict[str, Any]]] = {}
        for row in values:
            by_prop.setdefault(row["property_id"], []).append(row)

        blocks: list[str] = []
        for prop_id in sorted(by_prop):
            rows = by_prop[prop_id]
            table = []
            for r in rows:
                src = "—"
                if r.get("source_ref_id") is not None:
                    src = f"{r.get('source_kind') or 'ref'}:{r['source_ref_id']}"
                    if r.get("source_chunk"):
                        src += f"~{r['source_chunk']}"
                elif r.get("source_url"):
                    src = str(r["source_url"])[:40]
                table.append(
                    {
                        "value": _display_value(r),
                        "conditions": _fmt_conditions(r.get("conditions")) or "—",
                        "maturity": r.get("maturity") or "—",
                        "method": r.get("method") or "—",
                        "source": src,
                        # Rendered, not merely stored: an agent deciding
                        # whether a number may be published has to be able to
                        # SEE the licence. Unrecorded shows '—', which the
                        # skill defines as "treat as unpublishable".
                        "licence": r.get("source_licence") or "—",
                        "as_of": str(r["as_of"]) if r.get("as_of") else "—",
                    }
                )
            n = len(rows)
            grounded = sum(
                1
                for r in rows
                if r.get("source_ref_id") is not None or r.get("source_url")
            )
            blocks.append(
                f"## {prop_id}  ({n} report{'s' if n != 1 else ''}, "
                f"sourced: {grounded} of {n})\n"
                + render_agent_table(
                    table,
                    schema=[
                        "value",
                        "conditions",
                        "maturity",
                        "method",
                        "source",
                        "licence",
                        "as_of",
                    ],
                )
            )
        return Response(body="\n".join(head) + "\n\n" + "\n\n".join(blocks))

    # ── search ───────────────────────────────────────────────────────

    def search(
        self,
        *,
        q: str | None = None,
        property: str | None = None,
        min: float | None = None,
        max: float | None = None,
        maturity: str | None = None,
        reaction_class: str | None = None,
        page_size: int = 20,
        **_kw: Any,
    ) -> Response:
        if property is not None:
            return self._search_values(
                property_id=str(property).strip(),
                min_val=min,
                max_val=max,
                maturity=maturity,
                reaction_class=reaction_class,
                limit=page_size,
            )
        if q is None or not str(q).strip():
            raise BadInput(
                "search(kind='rxn') needs q= or property=",
                next=(
                    "search(kind='rxn', q='amide coupling') | "
                    "search(kind='rxn', property='yield', min=80)"
                ),
            )
        hits = self.store.rxn_search_entities(str(q).strip(), limit=page_size)
        if not hits:
            return Response(body=f"no rxn matches for {q!r}")
        rows = [
            {
                "rxn": slug or ref_id,
                "title": title,
                "smiles": fmt_smiles(
                    m.get("rxn_smiles") or m.get("rxn_smiles_raw") or ""
                ),
                "class": m.get("reaction_class") or "—",
            }
            for ref_id, slug, title, m in hits
        ]
        return Response(
            body=f"# {len(rows)} rxn match(es) for {q!r}\n"
            + render_agent_table(rows, schema=["rxn", "title", "smiles", "class"])
        )

    def _search_values(
        self,
        *,
        property_id: str,
        min_val: float | None,
        max_val: float | None,
        maturity: str | None,
        reaction_class: str | None,
        limit: int,
    ) -> Response:
        prop = self.store.rxn_property_get(property_id)
        if prop is None:
            raise NotFound(
                f"unknown property {property_id!r}",
                next="get(kind='rxn', view='properties') to see the registry",
            )
        if maturity is not None and maturity not in _MATURITIES:
            raise BadInput(f"maturity={maturity!r} must be one of {list(_MATURITIES)}")
        rows = self.store.rxn_search_values(
            property_id=property_id,
            min_val=min_val,
            max_val=max_val,
            maturity=maturity,
            reaction_class=reaction_class,
            limit=limit,
        )
        if not rows:
            return Response(body=f"no {property_id} values match")
        unit = prop.get("canonical_unit") or ""
        table = [
            {
                "rxn": r.get("rxn_title") or r["rxn_ref_id"],
                "value": _display_value(r),
                "conditions": _fmt_conditions(r.get("conditions")) or "—",
                "method": r.get("method") or "—",
                "maturity": r.get("maturity") or "—",
                "licence": r.get("source_licence") or "—",
            }
            for r in rows
        ]
        head = f"# {property_id}"
        if unit:
            head += f" ({unit})"
        head += f" — {len(rows)} value(s)"
        if reaction_class:
            head += f" in class {reaction_class}"
        return Response(
            body=head
            + "\n"
            + render_agent_table(
                table,
                schema=["rxn", "value", "conditions", "method", "maturity", "licence"],
            )
            + "\n\nEvery reported value is shown — the spread is the finding, "
            "not an average."
        )
