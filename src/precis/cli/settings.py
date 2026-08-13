"""``precis settings`` — operate the DB-resident config layer
(:mod:`precis.settings`), sibling of ``precis secret``.

Subcommands:

* ``list``          — registered inventory: key, resolved value, layer
  (db/env/default), env var, ``updated_at``/``updated_by`` when a DB row
  exists.
* ``get KEY``        — one key's resolved value + the layer it came from.
* ``set KEY VALUE``  — write a DB override. Refuses a key that isn't in
  :data:`precis.settings.REGISTRY` (killing the key-typo-silently-defaults
  failure mode the registry exists to prevent) and validates ``VALUE``
  against the registered type *before* writing.
* ``clear KEY``      — delete the DB row, reverting to the env / compiled
  default.
"""

from __future__ import annotations

import argparse
import sys

from precis.cli._common import resolve_dsn
from precis.settings import SettingSpec


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = sub.add_parser(
        "settings", help="Manage DB-resident settings (precis.settings)."
    )
    parser.add_argument(
        "--database-url", default=None, help="Override PRECIS_DATABASE_URL."
    )
    s = parser.add_subparsers(dest="settings_cmd", required=True)

    s.add_parser("list", help="Registered inventory + resolved value/layer.")

    p_get = s.add_parser("get", help="Resolve one key's value + layer.")
    p_get.add_argument("key")

    p_set = s.add_parser("set", help="Write a DB override for a registered key.")
    p_set.add_argument("key")
    p_set.add_argument("value")

    p_clear = s.add_parser(
        "clear", help="Delete a DB override (revert to env/compiled default)."
    )
    p_clear.add_argument("key")

    return parser


def run(args: argparse.Namespace) -> None:
    from precis import settings as psettings
    from precis.store import Store

    store = Store.connect(resolve_dsn(getattr(args, "database_url", None)))
    try:
        cmd = args.settings_cmd
        if cmd == "list":
            _list(store, psettings)
        elif cmd == "get":
            _get(args, store, psettings)
        elif cmd == "set":
            _set(args, store, psettings)
        elif cmd == "clear":
            psettings.clear_setting(args.key, store=store)
            print(f"settings: cleared {args.key} — reverts to env/compiled default")
    finally:
        store.close()


def _list(store: object, psettings: object) -> None:
    rows = psettings.list_settings(store=store)  # type: ignore[attr-defined]
    if not rows:
        print("settings: no registered keys")
        return
    width = max(len(str(r["key"])) for r in rows)
    for r in rows:
        meta = ""
        if r.get("updated_at") is not None:
            by = f" by {r['updated_by']}" if r.get("updated_by") else ""
            meta = f"  (updated {r['updated_at']}{by})"
        env_var = r["env_var"] or "-"
        print(
            f"{r['key']!s:<{width}}  {r['value']!s:<12}  {r['layer']:<8}  "
            f"env={env_var}{meta}"
        )


def _get(args: argparse.Namespace, store: object, psettings: object) -> None:
    value, layer = psettings.resolve(args.key, store=store)  # type: ignore[attr-defined]
    print(f"{args.key} = {value} (layer={layer})")


def _set(args: argparse.Namespace, store: object, psettings: object) -> None:
    entry = psettings.REGISTRY.get(args.key)  # type: ignore[attr-defined]
    if entry is None:
        print(
            f"settings: unregistered key {args.key!r} — refusing to set. "
            "Register it in precis.settings.REGISTRY first.",
            file=sys.stderr,
        )
        sys.exit(2)
    try:
        coerced = coerce_for_write(entry, args.value)
    except ValueError as exc:
        print(f"settings: {exc}", file=sys.stderr)
        sys.exit(2)
    psettings.set_setting(args.key, coerced, store=store)  # type: ignore[attr-defined]
    print(f"settings: set {args.key} = {coerced} (db)")


#: Same acceptance set precis.settings._coerce_bool recognises — mirrored
#: here (not imported) so this validation stays a pure function the web
#: route can share without importing precis.settings internals.
_TRUE_STRINGS = frozenset({"1", "true", "yes", "on"})
_FALSE_STRINGS = frozenset({"0", "false", "no", "off"})


def coerce_for_write(entry: SettingSpec, raw: str) -> object:
    """Validate + coerce a raw CLI/web string against ``entry``'s registered
    type. Raises :class:`ValueError` (message safe to show the operator) on
    an unparsable value — used to refuse a bad ``set`` *before* it writes,
    rather than storing a string ``get_float``/``get_bool`` would later warn
    about and silently drop."""
    if entry.type == "str":
        return raw
    if entry.type == "bool":
        s = raw.strip().lower()
        if s in _TRUE_STRINGS:
            return True
        if s in _FALSE_STRINGS:
            return False
        raise ValueError(
            f"{raw!r} is not a valid bool for {entry.key} "
            f"(accepts {sorted(_TRUE_STRINGS | _FALSE_STRINGS)})"
        )
    if entry.type == "float":
        try:
            return float(raw)
        except ValueError:
            raise ValueError(f"{raw!r} is not a valid float for {entry.key}") from None
    if entry.type == "int":
        try:
            return int(raw)
        except ValueError:
            raise ValueError(f"{raw!r} is not a valid int for {entry.key}") from None
    return raw  # pragma: no cover - SettingType is exhaustive above


__all__ = ["add_parser", "coerce_for_write", "run"]
