"""Shared base for the value-entity kinds (``material`` and ``component``).

Both kinds are the same star schema — a slug entity, a typed registry
(``property`` / ``spec``) and an append-only table of sourced values — so the
value-write path is one implementation: ``put(id=<slug>, <registry key>=...,
value=...)`` validates the type args, resolves or mints the registry row,
checks the canonical unit, routes ``value=`` onto the typed columns and
resolves ``source=``/``chunk=``.

Subclass contract (what genuinely differs per kind):

    ClassVar ``_VE_KIND`` / ``_VE_ARG`` / ``_VE_ID`` / ``_VE_PLURAL`` / ``_VE_METHODS``
        kind name, the put kwarg naming the registry row (``property`` /
        ``spec``), that row's id column (``prop_id`` / ``spec_id``), the
        registry noun's plural and the allowed ``method=`` vocabulary
    ``_VE_CREATE_HINT``
        the "create the entity first" ``next=`` template (``{slug}`` filled in)
    ``_ve_registry_row``
        get-or-mint the registry row (component also applies its category
        scoping here)
    ``_ve_insert``
        the store's value INSERT with the kind's own kwarg names

Error text is built from those attributes so each kind's messages are
byte-identical to what the pre-extraction per-kind copies produced.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from precis.errors import BadInput, NotFound
from precis.handlers._link_target import LinkTarget, parse_link_target
from precis.protocol import Handler
from precis.response import Response
from precis.store import Store
from precis.utils import handle_registry

_MATURITIES: tuple[str, ...] = ("commercial", "lab", "speculative")
_SOURCE_KINDS: tuple[str, ...] = ("paper", "datasheet")
_VALUE_TYPES: tuple[str, ...] = ("quantity", "ratio", "categorical", "boolean", "text")


def coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("true", "yes", "1"):
            return True
        if s in ("false", "no", "0"):
            return False
    return None


def display_value(v: Mapping[str, Any]) -> str:
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
        return str(v["value_text"])
    return "—"


def fmt_conditions(conditions: Any) -> str:
    if not conditions:
        return ""
    return ", ".join(f"{k}={v}" for k, v in conditions.items())


class ValueEntityHandler(Handler):
    """Value-write path shared by ``MaterialHandler`` / ``ComponentHandler``."""

    store: Store

    _VE_KIND: ClassVar[str]
    _VE_ARG: ClassVar[str]
    _VE_ID: ClassVar[str]
    _VE_PLURAL: ClassVar[str]
    _VE_METHODS: ClassVar[tuple[str, ...]]
    _VE_CREATE_HINT: ClassVar[str]

    # ── per-kind hooks ───────────────────────────────────────────────

    def _ve_registry_row(
        self,
        entity_ref: Any,
        key: str,
        *,
        value: Any,
        unit: str | None,
        value_type: str | None,
        allowed_values: list[Any] | None,
    ) -> Mapping[str, Any]:
        raise NotImplementedError

    def _ve_insert(
        self,
        entity_ref_id: int,
        row: Mapping[str, Any],
        **kwargs: Any,
    ) -> int:
        raise NotImplementedError

    # ── put ──────────────────────────────────────────────────────────

    def _put_value(
        self,
        slug: str,
        *,
        key: str,
        value: Any,
        unit: str | None,
        conditions: dict[str, Any] | None,
        maturity: str | None,
        method: str | None = None,
        source: str | None,
        chunk: str | None,
        as_of: str | None = None,
        value_type: str | None = None,
        allowed_values: list[Any] | None = None,
        value_low: float | None = None,
        value_high: float | None = None,
    ) -> Response:
        kind, arg = self._VE_KIND, self._VE_ARG
        if not key:
            raise BadInput(
                f"put(kind={kind!r}) with {arg}= needs a non-empty {self._VE_ID}",
                next=f"get(kind={kind!r}, view={self._VE_PLURAL!r}) to see the registry",
            )
        entity_ref = self.store.get_ref(kind=kind, id=slug)
        if entity_ref is None:
            raise NotFound(
                f"{kind} {slug!r} not found - create the entity first",
                next=self._VE_CREATE_HINT.format(slug=repr(slug)),
            )
        if conditions is not None and not isinstance(conditions, dict):
            raise BadInput(f"put(kind={kind!r}) conditions= must be a dict")

        self._validate_type_args(value_type, allowed_values)

        row = self._ve_registry_row(
            entity_ref,
            key,
            value=value,
            unit=unit,
            value_type=value_type,
            allowed_values=allowed_values,
        )

        self._check_unit(row, unit)
        value_kwargs = self._route_value(
            row, value, value_low=value_low, value_high=value_high
        )

        if maturity is not None and maturity not in _MATURITIES:
            raise BadInput(
                f"maturity={maturity!r} must be one of {list(_MATURITIES)}",
                next=f"put(kind={kind!r}, id={slug!r}, {arg}={key!r}, "
                f"value=..., maturity='lab')",
            )
        if method is not None and method not in self._VE_METHODS:
            raise BadInput(
                f"method={method!r} must be one of {list(self._VE_METHODS)}",
                next=f"put(kind={kind!r}, id={slug!r}, {arg}={key!r}, "
                f"value=..., method='measured')",
            )

        source_ref_id, source_chunk, source_url = self._resolve_source(source, chunk)

        value_id = self._ve_insert(
            entity_ref.id,
            row,
            conditions=conditions,
            maturity=maturity or "lab",
            method=method,
            source_ref_id=source_ref_id,
            source_chunk=source_chunk,
            source_url=source_url,
            as_of=as_of,
            **value_kwargs,
        )
        shown = display_value(
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
                f"recorded {slug}.{row[self._VE_ID]} = {shown}{unit_note} "
                f"(id={value_id}, maturity={maturity or 'lab'})"
                f"{source_note}"
            )
        )

    @classmethod
    def _validate_type_args(
        cls, value_type: str | None, allowed_values: list[Any] | None
    ) -> None:
        """Validate ``value_type=``/``allowed_values=`` shape, independent
        of whether this write mints a fresh registry row or targets an
        existing one (``_check_type_consistency`` covers the latter)."""
        if value_type is not None and value_type not in _VALUE_TYPES:
            raise BadInput(
                f"value_type={value_type!r} must be one of {list(_VALUE_TYPES)}",
            )
        if allowed_values is not None and value_type != "categorical":
            raise BadInput(
                "allowed_values= is only valid with value_type='categorical'",
                next=(
                    f"put(kind={cls._VE_KIND!r}, id=<slug>, "
                    f"{cls._VE_ARG}=<{cls._VE_ID}>, "
                    "value=..., value_type='categorical', "
                    "allowed_values=['a', 'b'])"
                ),
            )

    @classmethod
    def _check_unit(cls, row: Mapping[str, Any], unit: str | None) -> None:
        row_id = row[cls._VE_ID]
        canonical = row.get("canonical_unit")
        given = None if unit is None else (str(unit).strip() or None)
        if canonical is not None:
            if given != canonical:
                raise BadInput(
                    f"unit={unit!r} is not {row_id}'s canonical "
                    f"unit ({canonical!r}) - v1 is canonical-unit-only, "
                    "no conversion",
                    next=(
                        f"put(kind={cls._VE_KIND!r}, id=<slug>, "
                        f"{cls._VE_ARG}={row_id!r}, value=..., "
                        f"unit={canonical!r})"
                    ),
                )
        elif given is not None:
            raise BadInput(
                f"{row_id} has no canonical unit "
                "(dimensionless/categorical/boolean/text) - drop unit=",
                next=(
                    f"put(kind={cls._VE_KIND!r}, id=<slug>, "
                    f"{cls._VE_ARG}={row_id!r}, value=...)"
                ),
            )

    @classmethod
    def _route_value(
        cls,
        row: Mapping[str, Any],
        value: Any,
        *,
        value_low: float | None = None,
        value_high: float | None = None,
    ) -> dict[str, Any]:
        row_id = row[cls._VE_ID]
        noun, plural = cls._VE_ARG, cls._VE_PLURAL
        value_type = row["value_type"]
        has_band = value_low is not None or value_high is not None

        if has_band and value_type not in ("quantity", "ratio"):
            raise BadInput(
                f"{row_id} is a {value_type} {noun} - value_low=/value_high= "
                f"apply only to numeric (quantity/ratio) {plural}",
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
                        f"{row_id} is a {value_type} {noun} - value= must be "
                        f"numeric, got {value!r}"
                    )
                try:
                    num = float(value)
                except (TypeError, ValueError):
                    raise BadInput(
                        f"{row_id} is a {value_type} {noun} - value= must be "
                        f"numeric, got {value!r}"
                    ) from None
            elif value_low is not None and value_high is not None:
                num = (float(value_low) + float(value_high)) / 2
            elif has_band:
                raise BadInput(
                    f"put(kind={cls._VE_KIND!r}, {noun}={row_id!r}) needs "
                    "value=, or both value_low= and value_high=",
                )
            else:
                raise BadInput(
                    f"put(kind={cls._VE_KIND!r}, {noun}={row_id!r}) needs value=",
                )
            out: dict[str, Any] = {"value_num": num}
            if value_low is not None:
                out["value_low"] = float(value_low)
            if value_high is not None:
                out["value_high"] = float(value_high)
            return out
        if value is None:
            raise BadInput(
                f"put(kind={cls._VE_KIND!r}, {noun}={row_id!r}) needs value=",
            )
        if value_type == "boolean":
            b = coerce_bool(value)
            if b is None:
                raise BadInput(
                    f"{row_id} is boolean - value= must be true/false, got {value!r}"
                )
            return {"value_bool": b}
        if value_type == "categorical":
            s = str(value).strip()
            allowed = row.get("allowed_values") or []
            if allowed and s not in allowed:
                raise BadInput(
                    f"{row_id} value {s!r} is not in allowed_values {allowed!r}",
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
        kind, arg = self._VE_KIND, self._VE_ARG
        if source is None or not str(source).strip():
            if chunk is not None:
                raise BadInput(
                    "chunk= requires source= (the ref the chunk belongs to)",
                    next=f"put(kind={kind!r}, id=<slug>, {arg}=..., "
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
                f"{kind} sources must be one of {list(_SOURCE_KINDS)}, or "
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
