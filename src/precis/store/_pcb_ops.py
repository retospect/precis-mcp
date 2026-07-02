"""Store ops for the ``pcb`` kind (ADR 0042).

The design is a slug-addressed ``refs`` row (``kind='pcb'``) keeping **one**
``card_combined`` chunk for intent-search; the graph lives in the dedicated
``pcb_*`` tables (a normalized type/instance model):

- ``pcb_components`` — a component *type* (owns its pins).
- ``pcb_pins``       — pins of a type (pad + function name + electrical tags).
- ``pcb_instances``  — a placement (refdes) of a component.
- ``pcb_nets``       — a named, classed signal.
- ``pcb_netconns``   — the netlist triple (net, instance, pin); a physical pin
  is on <=1 net; composite FKs force the pin and the instance to share a
  component.

Authoring is **batch**: :meth:`pcb_apply` lays down components+pins+instances,
nets, and connections for a design in one transaction and is *re-runnable*
(existing refdes/net names are reused, not duplicated). Reads (:meth:`pcb_load`,
:meth:`pcb_instance_neighbors`, :meth:`pcb_net_members`) back the graph-
traversal surface; the derived layer (ratsnest/crossings) is computed by the
handler, not stored.

Mixin assumes the concrete Store provides ``self.pool`` / ``self.tx`` /
``self.insert_ref`` / ``self.get_ref``.
"""

from __future__ import annotations

from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb


def _jsonb_or_none(value: Any) -> Jsonb | None:
    """psycopg Jsonb for a nullable JSONB column."""
    return Jsonb(value) if value is not None else None


class PcbMixin:
    pool: Any
    tx: Any
    insert_ref: Any
    get_ref: Any
    soft_delete_ref: Any  # RefsMixin — the shared ref soft-delete
    _replace_card_combined: Any  # BlocksMixin — the shared card_combined write

    # -- write ----------------------------------------------------------
    def pcb_apply(
        self,
        *,
        slug: str,
        title: str,
        components: list[dict[str, Any]],
        nets: list[dict[str, Any]],
        connections: list[dict[str, Any]],
        measures: list[dict[str, Any]] | None = None,
        features: list[dict[str, Any]] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> tuple[Any, bool, dict[str, int]]:
        """Create-or-extend a design, batch. Returns ``(ref, created, counts)``.

        Each *component* dict creates a component TYPE + its pins + **one**
        instance (the 1:1 convenience, ADR 0042 §4 A4): keys ``refdes`` (req),
        ``label``, ``part``/``part_lcsc``, ``footprint``, ``courtyard``,
        ``centroid``, ``height_mm``, ``x``, ``y``, ``rot``, ``layer``,
        ``fixed``, ``roles``, ``note``, and ``pins`` (a list of
        ``{name, pad?, tags?, description?, note?}``). A *net* dict: ``name``
        (req), ``net_class``/``class``, ``est_current_a``/``current``,
        ``width_mm``/``width``, ``note``. A *connection* dict: ``net`` (req),
        ``refdes`` (req), ``pin`` (req, a pin name), ``note``. Re-runnable:
        existing refdes / net names are reused.
        """
        existing = self.get_ref(kind="pcb", id=slug)
        created = existing is None
        counts = {
            "components": 0,
            "pins": 0,
            "instances": 0,
            "nets": 0,
            "conns": 0,
            "measures": 0,
            "features": 0,
        }
        with self.tx() as conn:
            if created:
                ref = self.insert_ref(
                    kind="pcb",
                    slug=slug,
                    title=title,
                    meta=dict(meta or {}),
                    conn=conn,
                )
            else:
                ref = existing
                conn.execute(
                    "UPDATE refs SET title = %s WHERE ref_id = %s",
                    (title, ref.id),
                )
                if meta is not None:
                    conn.execute(
                        "UPDATE refs SET meta = meta || %s WHERE ref_id = %s",
                        (Jsonb(dict(meta)), ref.id),
                    )

            # refdes -> (instance_id, component_id); net name -> net_id
            inst_by_refdes = self._pcb_instance_map(conn, ref.id)
            net_by_name = self._pcb_net_map(conn, ref.id)

            # catalog snapshots for the whole batch up front (two ANY()
            # queries) — resolving inside the loop was 2 queries per component.
            part_cache = self._pcb_resolve_parts(
                conn,
                sorted(
                    {
                        str(c.get("part_lcsc") or c.get("part")).strip().upper()
                        for c in components
                        if (c.get("part_lcsc") or c.get("part"))
                    }
                ),
            )

            for c in components:
                refdes = str(c.get("refdes") or "").strip()
                if not refdes:
                    raise ValueError("pcb component needs a refdes")
                if refdes in inst_by_refdes:
                    continue  # already placed; skip (re-runnable)
                comp_id = self._pcb_insert_component(conn, ref.id, c, part_cache)
                counts["components"] += 1
                counts["pins"] += self._pcb_insert_pins(
                    conn, comp_id, c.get("pins") or []
                )
                inst_id = self._pcb_insert_instance(conn, ref.id, comp_id, refdes, c)
                counts["instances"] += 1
                inst_by_refdes[refdes] = (inst_id, comp_id)

            for n in nets:
                name = str(n.get("name") or "").strip()
                if not name:
                    raise ValueError("pcb net needs a name (meaningful)")
                if name in net_by_name:
                    continue
                net_by_name[name] = self._pcb_insert_net(conn, ref.id, n)
                counts["nets"] += 1

            for k in connections:
                counts["conns"] += self._pcb_connect(
                    conn, ref.id, k, inst_by_refdes, net_by_name
                )

            for mm in measures or []:
                self._pcb_insert_measure(conn, ref.id, mm)
                counts["measures"] += 1

            for ft in features or []:
                self._pcb_insert_feature(conn, ref.id, ft)
                counts["features"] += 1

            self._replace_card_combined(
                conn,
                ref_id=ref.id,
                card_text=self._pcb_card_text(conn, ref.id, title),
            )
        return ref, created, counts

    def _pcb_card_text(self, conn: Connection, ref_id: int, title: str) -> str:
        """The one embeddable summary per design — built from the current graph
        (component labels + a few net names) so search lands on intent."""
        labels = [
            str(r[0])
            for r in conn.execute(
                "SELECT DISTINCT label FROM pcb_components "
                "WHERE ref_id = %s AND retired_at IS NULL ORDER BY label",
                (ref_id,),
            ).fetchall()
        ]
        n_inst = conn.execute(
            "SELECT count(*) FROM pcb_instances "
            "WHERE ref_id = %s AND retired_at IS NULL",
            (ref_id,),
        ).fetchone()
        nets = [
            str(r[0])
            for r in conn.execute(
                "SELECT name FROM pcb_nets WHERE ref_id = %s AND retired_at IS NULL "
                "ORDER BY name LIMIT 8",
                (ref_id,),
            ).fetchall()
        ]
        n_parts = int(n_inst[0]) if n_inst else 0
        return (
            f"{title} (PCB design). {n_parts} parts: {', '.join(labels)}. "
            f"Nets: {', '.join(nets)}."
        )

    # -- write helpers --------------------------------------------------
    def _pcb_insert_component(
        self,
        conn: Connection,
        ref_id: int,
        c: dict[str, Any],
        part_cache: dict[str, dict[str, Any]],
    ) -> int:
        part_lcsc = c.get("part_lcsc") or c.get("part")
        footprint = c.get("footprint")
        height_mm = c.get("height_mm")
        courtyard = c.get("courtyard")
        # Auto-stamp from the catalog (ADR 0042 §5): if a C-number is given but
        # the snapshot fields are not, copy them from parts / part_footprints so
        # the design is self-contained even if the catalog later churns.
        if part_lcsc and (footprint is None or height_mm is None or courtyard is None):
            resolved = part_cache.get(str(part_lcsc).strip().upper())
            if resolved is not None:
                footprint = footprint or resolved.get("footprint")
                height_mm = (
                    height_mm if height_mm is not None else resolved.get("height_mm")
                )
                courtyard = (
                    courtyard if courtyard is not None else resolved.get("courtyard")
                )
        row = conn.execute(
            """
            INSERT INTO pcb_components
                (ref_id, label, part_lcsc, footprint, courtyard, centroid,
                 height_mm, note)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING component_id
            """,
            (
                ref_id,
                str(c.get("label") or c.get("refdes") or "part"),
                part_lcsc,
                footprint,
                _jsonb_or_none(courtyard),
                _jsonb_or_none(c.get("centroid")),
                height_mm,
                c.get("note"),
            ),
        ).fetchone()
        assert row is not None
        return int(row[0])

    def _pcb_resolve_parts(
        self, conn: Connection, lcscs: list[str]
    ) -> dict[str, dict[str, Any]]:
        """Footprint / height / courtyard snapshots for a batch of catalog
        C-numbers (parts.package + height, part_footprints.courtyard if
        cached) — two ``ANY()`` queries for the whole ``pcb_apply`` batch."""
        if not lcscs:
            return {}
        out: dict[str, dict[str, Any]] = {}
        for r in conn.execute(
            "SELECT lcsc, package, height_mm FROM parts WHERE lcsc = ANY(%s)",
            (lcscs,),
        ).fetchall():
            out[str(r[0])] = {
                "footprint": r[1],
                "height_mm": r[2],
                "courtyard": None,
            }
        for r in conn.execute(
            "SELECT lcsc, courtyard FROM part_footprints WHERE lcsc = ANY(%s)",
            (lcscs,),
        ).fetchall():
            entry = out.get(str(r[0]))
            if entry is not None:
                entry["courtyard"] = r[1]
        return out

    def _pcb_insert_pins(
        self, conn: Connection, component_id: int, pins: list[dict[str, Any]]
    ) -> int:
        n = 0
        for p in pins:
            name = str(p.get("name") or "").strip()
            if not name:
                continue
            conn.execute(
                """
                INSERT INTO pcb_pins
                    (component_id, pad, name, tags, description, note)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    component_id,
                    p.get("pad"),
                    name,
                    list(p.get("tags") or []),
                    p.get("description"),
                    p.get("note"),
                ),
            )
            n += 1
        return n

    def _pcb_insert_instance(
        self,
        conn: Connection,
        ref_id: int,
        component_id: int,
        refdes: str,
        c: dict[str, Any],
    ) -> int:
        row = conn.execute(
            """
            INSERT INTO pcb_instances
                (ref_id, component_id, refdes, x, y, rot, layer, fixed, roles, note)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING instance_id
            """,
            (
                ref_id,
                component_id,
                refdes,
                c.get("x"),
                c.get("y"),
                float(c.get("rot") or 0.0),
                str(c.get("layer") or "top"),
                c.get("fixed"),
                list(c.get("roles") or []),
                c.get("note"),
            ),
        ).fetchone()
        assert row is not None
        return int(row[0])

    def _pcb_insert_net(self, conn: Connection, ref_id: int, n: dict[str, Any]) -> int:
        row = conn.execute(
            """
            INSERT INTO pcb_nets
                (ref_id, name, net_class, est_current_a, width_mm, note)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING net_id
            """,
            (
                ref_id,
                str(n["name"]).strip(),
                n.get("net_class") or n.get("class"),
                n.get("est_current_a") or n.get("current"),
                n.get("width_mm") or n.get("width"),
                n.get("note"),
            ),
        ).fetchone()
        assert row is not None
        return int(row[0])

    def _pcb_insert_measure(
        self, conn: Connection, ref_id: int, m: dict[str, Any]
    ) -> None:
        metric = str(m.get("metric") or "").strip()
        if not metric:
            raise ValueError("pcb measure needs a metric")
        conn.execute(
            """
            INSERT INTO pcb_measures
                (ref_id, metric, direction, goal, strength, weight, operands,
                 reason)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                ref_id,
                metric,
                m.get("direction"),
                m.get("goal"),
                str(m.get("strength") or "gauge"),
                m.get("weight"),
                Jsonb(list(m.get("operands") or [])),
                m.get("reason"),
            ),
        )

    def _pcb_insert_feature(
        self, conn: Connection, ref_id: int, f: dict[str, Any]
    ) -> None:
        """A non-electrical placed feature (ADR 0042 §4): mounting hole /
        fiducial / testpoint / keepout / outline. ``geom`` carries the shape
        (hole ``diameter``, outline ``path`` of [x,y] points, keepout poly) in
        mm — read by the mechanical exporter (the 0041 bridge, §6)."""
        ftype = str(f.get("ftype") or f.get("type") or "").strip()
        if not ftype:
            raise ValueError("pcb feature needs an ftype")
        geom = f.get("geom")
        conn.execute(
            """
            INSERT INTO pcb_features
                (ref_id, ftype, x, y, rot, layer, fixed, geom, note)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                ref_id,
                ftype,
                f.get("x"),
                f.get("y"),
                float(f.get("rot") or 0.0),
                f.get("layer"),
                f.get("fixed"),
                _jsonb_or_none(geom),
                f.get("note"),
            ),
        )

    def _pcb_connect(
        self,
        conn: Connection,
        ref_id: int,
        k: dict[str, Any],
        inst_by_refdes: dict[str, tuple[int, int]],
        net_by_name: dict[str, int],
    ) -> int:
        net_name = str(k.get("net") or "").strip()
        refdes = str(k.get("refdes") or "").strip()
        pin_name = str(k.get("pin") or "").strip()
        if not (net_name and refdes and pin_name):
            raise ValueError("pcb connection needs net, refdes, and pin")
        if net_name not in net_by_name:
            # auto-create a net so wiring never silently drops (name is the meaning)
            net_by_name[net_name] = self._pcb_insert_net(
                conn, ref_id, {"name": net_name}
            )
        if refdes not in inst_by_refdes:
            raise ValueError(f"pcb connection references unknown refdes {refdes!r}")
        net_id = net_by_name[net_name]
        inst_id, comp_id = inst_by_refdes[refdes]
        pin_id = self._pcb_pin_id(conn, comp_id, pin_name)
        conn.execute(
            """
            INSERT INTO pcb_netconns (net_id, instance_id, pin_id, component_id, note)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (instance_id, pin_id) DO UPDATE SET net_id = EXCLUDED.net_id
            """,
            (net_id, inst_id, pin_id, comp_id, k.get("note")),
        )
        return 1

    def _pcb_pin_id(self, conn: Connection, component_id: int, name: str) -> int:
        """Resolve a pin by name within a component; create it ad-hoc if absent
        (ADR 0042 §4 — pins may be created during logical wiring)."""
        row = conn.execute(
            "SELECT pin_id FROM pcb_pins "
            "WHERE component_id = %s AND name = %s AND retired_at IS NULL",
            (component_id, name),
        ).fetchone()
        if row is not None:
            return int(row[0])
        new = conn.execute(
            "INSERT INTO pcb_pins (component_id, name) VALUES (%s, %s) "
            "RETURNING pin_id",
            (component_id, name),
        ).fetchone()
        assert new is not None
        return int(new[0])

    def _pcb_instance_map(
        self, conn: Connection, ref_id: int
    ) -> dict[str, tuple[int, int]]:
        rows = conn.execute(
            "SELECT refdes, instance_id, component_id FROM pcb_instances "
            "WHERE ref_id = %s AND retired_at IS NULL",
            (ref_id,),
        ).fetchall()
        return {str(r[0]): (int(r[1]), int(r[2])) for r in rows}

    def _pcb_net_map(self, conn: Connection, ref_id: int) -> dict[str, int]:
        rows = conn.execute(
            "SELECT name, net_id FROM pcb_nets "
            "WHERE ref_id = %s AND retired_at IS NULL",
            (ref_id,),
        ).fetchall()
        return {str(r[0]): int(r[1]) for r in rows}

    # -- read -----------------------------------------------------------
    def pcb_load(self, ref_id: int) -> dict[str, list[dict[str, Any]]]:
        """The design's instances + nets + a fanout count per net, for the
        netlist TOC. Components/pins are joined into the instance rows."""
        with self.pool.connection() as conn:
            instances = [
                {
                    "instance_id": int(r[0]),
                    "refdes": r[1],
                    "label": r[2],
                    "part_lcsc": r[3],
                    "footprint": r[4],
                    "layer": r[5],
                    "x": r[6],
                    "y": r[7],
                    "rot": r[8],
                    "fixed": r[9],
                    "roles": list(r[10] or []),
                    "note": r[11],
                    "height_mm": r[12],
                }
                for r in conn.execute(
                    "SELECT i.instance_id, i.refdes, c.label, c.part_lcsc, "
                    "       c.footprint, i.layer, i.x, i.y, i.rot, i.fixed, "
                    "       i.roles, i.note, c.height_mm "
                    "FROM pcb_instances i JOIN pcb_components c "
                    "  ON c.component_id = i.component_id "
                    "WHERE i.ref_id = %s AND i.retired_at IS NULL "
                    "ORDER BY i.refdes",
                    (ref_id,),
                ).fetchall()
            ]
            nets = [
                {
                    "net_id": int(r[0]),
                    "name": r[1],
                    "net_class": r[2],
                    "est_current_a": r[3],
                    "width_mm": r[4],
                    "note": r[5],
                    "fanout": int(r[6]),
                }
                for r in conn.execute(
                    "SELECT n.net_id, n.name, n.net_class, n.est_current_a, "
                    "       n.width_mm, n.note, count(k.netconn_id) "
                    "FROM pcb_nets n LEFT JOIN pcb_netconns k ON k.net_id = n.net_id "
                    "WHERE n.ref_id = %s AND n.retired_at IS NULL "
                    "GROUP BY n.net_id ORDER BY count(k.netconn_id) DESC, n.name",
                    (ref_id,),
                ).fetchall()
            ]
        return {"instances": instances, "nets": nets}

    def pcb_instance_neighbors(self, ref_id: int, refdes: str) -> dict[str, Any] | None:
        """The graph hop from one component instance: its pins, the net on each
        pin, and the neighbouring instances on those nets (ADR 0042 §8.2)."""
        with self.pool.connection() as conn:
            inst = conn.execute(
                "SELECT instance_id, component_id FROM pcb_instances "
                "WHERE ref_id = %s AND refdes = %s AND retired_at IS NULL",
                (ref_id, refdes),
            ).fetchone()
            if inst is None:
                return None
            inst_id, comp_id = int(inst[0]), int(inst[1])
            pins = [
                {
                    "pin": r[0],
                    "pad": r[1],
                    "tags": list(r[2] or []),
                    "net": r[3],
                    "neighbors": [n for n in (r[4] or []) if n and n != refdes],
                }
                for r in conn.execute(
                    """
                    SELECT p.name, p.pad, p.tags, n.name,
                           array_agg(DISTINCT ni.refdes)
                    FROM pcb_pins p
                    LEFT JOIN pcb_netconns k
                       ON k.pin_id = p.pin_id AND k.instance_id = %s
                    LEFT JOIN pcb_nets n ON n.net_id = k.net_id
                    LEFT JOIN pcb_netconns k2 ON k2.net_id = k.net_id
                    LEFT JOIN pcb_instances ni
                       ON ni.instance_id = k2.instance_id AND ni.retired_at IS NULL
                    WHERE p.component_id = %s AND p.retired_at IS NULL
                    GROUP BY p.pin_id, p.name, p.pad, p.tags, n.name
                    ORDER BY p.name
                    """,
                    (inst_id, comp_id),
                ).fetchall()
            ]
        return {"refdes": refdes, "pins": pins}

    def pcb_net_members(self, ref_id: int, name: str) -> dict[str, Any] | None:
        """A net's members: every (refdes, pin) on it (ADR 0042 §8.1)."""
        with self.pool.connection() as conn:
            net = conn.execute(
                "SELECT net_id, net_class, est_current_a, width_mm FROM pcb_nets "
                "WHERE ref_id = %s AND name = %s AND retired_at IS NULL",
                (ref_id, name),
            ).fetchone()
            if net is None:
                return None
            members = [
                {"refdes": r[0], "pin": r[1], "tags": list(r[2] or [])}
                for r in conn.execute(
                    "SELECT i.refdes, p.name, p.tags "
                    "FROM pcb_netconns k "
                    "JOIN pcb_instances i ON i.instance_id = k.instance_id "
                    "JOIN pcb_pins p ON p.pin_id = k.pin_id "
                    "WHERE k.net_id = %s ORDER BY i.refdes, p.name",
                    (int(net[0]),),
                ).fetchall()
            ]
        return {
            "name": name,
            "net_class": net[1],
            "est_current_a": net[2],
            "width_mm": net[3],
            "members": members,
        }

    def pcb_graph(self, ref_id: int) -> dict[str, Any]:
        """The whole design as the *eyes* (ADR 0042 §8) consume it: placed
        instances, nets with their (refdes, pin) members, and the unconnected
        pins. Pure data — the analysis lives in :mod:`precis.pcb`."""
        with self.pool.connection() as conn:
            instances = [
                {
                    "refdes": r[0],
                    "x": r[1],
                    "y": r[2],
                    "layer": r[3],
                    "roles": list(r[4] or []),
                    "label": r[5],
                    "height_mm": r[6],
                    "n_pins": int(r[7]),
                    "fixed": r[8],
                }
                for r in conn.execute(
                    "SELECT i.refdes, i.x, i.y, i.layer, i.roles, c.label, "
                    "       c.height_mm, "
                    "       (SELECT count(*) FROM pcb_pins p "
                    "        WHERE p.component_id = i.component_id "
                    "          AND p.retired_at IS NULL), i.fixed "
                    "FROM pcb_instances i JOIN pcb_components c "
                    "  ON c.component_id = i.component_id "
                    "WHERE i.ref_id = %s AND i.retired_at IS NULL "
                    "ORDER BY i.refdes",
                    (ref_id,),
                ).fetchall()
            ]
            net_rows = conn.execute(
                "SELECT net_id, name, net_class FROM pcb_nets "
                "WHERE ref_id = %s AND retired_at IS NULL ORDER BY name",
                (ref_id,),
            ).fetchall()
            nets = {
                int(r[0]): {"name": r[1], "net_class": r[2], "members": []}
                for r in net_rows
            }
            for r in conn.execute(
                "SELECT k.net_id, i.refdes, p.name "
                "FROM pcb_netconns k "
                "JOIN pcb_nets n ON n.net_id = k.net_id "
                "JOIN pcb_instances i ON i.instance_id = k.instance_id "
                "JOIN pcb_pins p ON p.pin_id = k.pin_id "
                "WHERE n.ref_id = %s AND n.retired_at IS NULL",
                (ref_id,),
            ).fetchall():
                nid = int(r[0])
                if nid in nets:
                    nets[nid]["members"].append({"refdes": r[1], "pin": r[2]})
            unconnected = [
                {"refdes": r[0], "pin": r[1]}
                for r in conn.execute(
                    "SELECT i.refdes, p.name "
                    "FROM pcb_instances i "
                    "JOIN pcb_pins p ON p.component_id = i.component_id "
                    "  AND p.retired_at IS NULL "
                    "WHERE i.ref_id = %s AND i.retired_at IS NULL "
                    "  AND NOT EXISTS (SELECT 1 FROM pcb_netconns k "
                    "                  WHERE k.instance_id = i.instance_id "
                    "                    AND k.pin_id = p.pin_id) "
                    "ORDER BY i.refdes, p.name",
                    (ref_id,),
                ).fetchall()
            ]
        return {
            "instances": instances,
            "nets": list(nets.values()),
            "unconnected": unconnected,
        }

    def pcb_set_placement(
        self,
        ref_id: int,
        placement: dict[str, tuple[float, float]],
        *,
        meta: dict[str, Any] | None = None,
    ) -> int:
        """Write new `(x, y)` for instances by refdes (ADR 0042 §9 placer).

        Never moves a `fixed` instance (guarded in SQL too). Optionally stamps
        a placement summary onto `refs.meta`. Returns the rows moved."""
        moved = 0
        with self.tx() as conn:
            for refdes, (x, y) in placement.items():
                moved += conn.execute(
                    "UPDATE pcb_instances SET x = %s, y = %s "
                    "WHERE ref_id = %s AND refdes = %s AND retired_at IS NULL "
                    "  AND (fixed IS NULL OR fixed NOT IN ('xy', 'both'))",
                    (float(x), float(y), ref_id, refdes),
                ).rowcount
            if meta is not None:
                conn.execute(
                    "UPDATE refs SET meta = meta || %s WHERE ref_id = %s",
                    (Jsonb(dict(meta)), ref_id),
                )
        return moved

    def pcb_measures_list(self, ref_id: int) -> list[dict[str, Any]]:
        """Live measures of a design (ADR 0042 §8.3)."""
        with self.pool.connection() as conn:
            return [
                {
                    "metric": r[0],
                    "direction": r[1],
                    "goal": r[2],
                    "strength": r[3],
                    "weight": r[4],
                    "operands": list(r[5] or []),
                    "reason": r[6],
                }
                for r in conn.execute(
                    "SELECT metric, direction, goal, strength, weight, operands, "
                    "       reason FROM pcb_measures "
                    "WHERE ref_id = %s AND retired_at IS NULL ORDER BY measure_id",
                    (ref_id,),
                ).fetchall()
            ]

    def pcb_features_list(self, ref_id: int) -> list[dict[str, Any]]:
        """Live non-electrical features of a design (ADR 0042 §4, §6) — the
        board outline + mounting holes the mechanical exporter / the 0041
        enclosure bridge consume."""
        with self.pool.connection() as conn:
            return [
                {
                    "feature_id": int(r[0]),
                    "ftype": r[1],
                    "x": r[2],
                    "y": r[3],
                    "rot": r[4],
                    "layer": r[5],
                    "fixed": r[6],
                    "geom": r[7],
                    "note": r[8],
                }
                for r in conn.execute(
                    "SELECT feature_id, ftype, x, y, rot, layer, fixed, geom, note "
                    "FROM pcb_features "
                    "WHERE ref_id = %s AND retired_at IS NULL ORDER BY feature_id",
                    (ref_id,),
                ).fetchall()
            ]

    def pcb_footprints_for(self, ref_id: int) -> dict[str, dict[str, Any]]:
        """Cached Flow-B footprints (pads + pin_map) keyed by C-number for every
        part the design's instances reference. The DSN exporter (§6) uses real
        pad geometry where present and falls back to centroid pins otherwise."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT f.lcsc, f.pads, f.pin_map, f.courtyard, f.centroid "
                "FROM part_footprints f "
                "WHERE f.lcsc IN ("
                "  SELECT DISTINCT c.part_lcsc FROM pcb_instances i "
                "  JOIN pcb_components c ON c.component_id = i.component_id "
                "  WHERE i.ref_id = %s AND i.retired_at IS NULL "
                "    AND c.part_lcsc IS NOT NULL)",
                (ref_id,),
            ).fetchall()
        return {
            str(r[0]): {
                "pads": r[1],
                "pin_map": r[2],
                "courtyard": r[3],
                "centroid": r[4],
            }
            for r in rows
        }

    # -- parts catalog (ADR 0042 §5) ------------------------------------
    def parts_import(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        """Upsert normalized catalog rows (:func:`precis.pcb.catalog.
        normalize_jlcparts_row`) + update the turnover signal. Returns
        ``{upserted, restocked}``.

        Upsert (not the atomic swap) keeps the table live and the FK-free
        ``part_footprints`` / ``part_availability`` caches intact; the
        staging + atomic-swap is the scale lever for the full ~300k dump
        (ADR 0042 §5 — "drop-index trick optional at our row count")."""
        counts = {"upserted": 0, "restocked": 0}
        with self.tx() as conn:
            for r in rows:
                lcsc = r["lcsc"]
                new_stock = int(r.get("stock") or 0)
                if self._parts_update_availability(conn, lcsc, new_stock):
                    counts["restocked"] += 1
                conn.execute(
                    """
                    INSERT INTO parts
                        (lcsc, mfr, mfr_part, description, jlcpcb_assemblable,
                         basic, stock, price, package, height_mm, params,
                         datasheet_url)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (lcsc) DO UPDATE SET
                        mfr=EXCLUDED.mfr, mfr_part=EXCLUDED.mfr_part,
                        description=EXCLUDED.description,
                        jlcpcb_assemblable=EXCLUDED.jlcpcb_assemblable,
                        basic=EXCLUDED.basic, stock=EXCLUDED.stock,
                        price=EXCLUDED.price, package=EXCLUDED.package,
                        height_mm=EXCLUDED.height_mm, params=EXCLUDED.params,
                        datasheet_url=EXCLUDED.datasheet_url, refreshed_at=now()
                    """,
                    (
                        lcsc,
                        r.get("mfr"),
                        r.get("mfr_part"),
                        r.get("description") or "",
                        bool(r.get("jlcpcb_assemblable", True)),
                        bool(r.get("basic", False)),
                        new_stock,
                        _jsonb_or_none(r.get("price")),
                        r.get("package"),
                        r.get("height_mm"),
                        _jsonb_or_none(r.get("params")),
                        r.get("datasheet_url"),
                    ),
                )
                counts["upserted"] += 1
        return counts

    def _parts_update_availability(
        self, conn: Connection, lcsc: str, new_stock: int
    ) -> bool:
        """Roll the turnover signal for one part; returns True if restocked
        (stock rose vs the previous dump)."""
        prev = conn.execute(
            "SELECT stock_now, ewma_stock FROM part_availability WHERE lcsc = %s",
            (lcsc,),
        ).fetchone()
        if prev is None:
            conn.execute(
                "INSERT INTO part_availability "
                "(lcsc, stock_now, stock_prev, ewma_stock, restock_count, trend) "
                "VALUES (%s, %s, %s, %s, 0, 0)",
                (lcsc, new_stock, new_stock, float(new_stock)),
            )
            return False
        old_stock = int(prev[0] or 0)
        old_ewma = float(prev[1] or 0.0)
        restocked = new_stock > old_stock
        conn.execute(
            "UPDATE part_availability SET stock_prev = stock_now, stock_now = %s, "
            "ewma_stock = %s, trend = %s, restock_count = restock_count + %s, "
            "last_restock_at = CASE WHEN %s THEN now() ELSE last_restock_at END, "
            "discontinued = false, updated_at = now() WHERE lcsc = %s",
            (
                new_stock,
                0.7 * old_ewma + 0.3 * new_stock,
                new_stock - old_stock,
                1 if restocked else 0,
                restocked,
                lcsc,
            ),
        )
        return restocked

    def parts_search(self, q: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """The JLCPCB-native selector (ADR 0042 §5): hard-filter to assemblable
        parts; rank Basic-first then **turnover** (restock frequency + healthy
        EWMA stock) — prefer parts that keep being available, not the last reel.
        """
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT p.lcsc, p.mfr_part, p.description, p.basic, p.stock, "
                "       p.package, p.price, coalesce(a.restock_count, 0), "
                "       a.ewma_stock "
                "FROM parts p LEFT JOIN part_availability a ON a.lcsc = p.lcsc "
                "WHERE p.jlcpcb_assemblable "
                "  AND p.description_tsv @@ plainto_tsquery('english', %s) "
                "ORDER BY p.basic DESC, coalesce(a.restock_count, 0) DESC, "
                "         coalesce(a.ewma_stock, p.stock, 0) DESC "
                "LIMIT %s",
                (q, limit),
            ).fetchall()
        return [
            {
                "lcsc": r[0],
                "mfr_part": r[1],
                "description": r[2],
                "basic": bool(r[3]),
                "stock": r[4],
                "package": r[5],
                "price": r[6],
                "restock_count": int(r[7]),
                "ewma_stock": r[8],
            }
            for r in rows
        ]

    def part_row(self, lcsc: str) -> dict[str, Any] | None:
        with self.pool.connection() as conn:
            r = conn.execute(
                "SELECT p.lcsc, p.mfr, p.mfr_part, p.description, "
                "       p.jlcpcb_assemblable, p.basic, p.stock, p.package, "
                "       p.height_mm, p.datasheet_url, a.restock_count, a.ewma_stock "
                "FROM parts p LEFT JOIN part_availability a ON a.lcsc = p.lcsc "
                "WHERE p.lcsc = %s",
                (lcsc,),
            ).fetchone()
        if r is None:
            return None
        return {
            "lcsc": r[0],
            "mfr": r[1],
            "mfr_part": r[2],
            "description": r[3],
            "jlcpcb_assemblable": bool(r[4]),
            "basic": bool(r[5]),
            "stock": r[6],
            "package": r[7],
            "height_mm": r[8],
            "datasheet_url": r[9],
            "restock_count": r[10],
            "ewma_stock": r[11],
        }

    def part_footprint_get(self, lcsc: str) -> dict[str, Any] | None:
        """The Flow B easyeda2kicad cache row for a C-number, or None."""
        with self.pool.connection() as conn:
            r = conn.execute(
                "SELECT pads, pin_map, courtyard, centroid, kicad_mod, source "
                "FROM part_footprints WHERE lcsc = %s",
                (lcsc,),
            ).fetchone()
        if r is None:
            return None
        return {
            "lcsc": lcsc,
            "pads": r[0],
            "pin_map": r[1],
            "courtyard": r[2],
            "centroid": r[3],
            "kicad_mod": r[4],
            "source": r[5],
        }

    def part_footprint_put(self, lcsc: str, data: dict[str, Any]) -> None:
        """Cache a converted footprint (Flow B). Upsert by C-number."""
        with self.tx() as conn:
            conn.execute(
                """
                INSERT INTO part_footprints
                    (lcsc, pads, pin_map, courtyard, centroid, kicad_mod, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (lcsc) DO UPDATE SET
                    pads=EXCLUDED.pads, pin_map=EXCLUDED.pin_map,
                    courtyard=EXCLUDED.courtyard, centroid=EXCLUDED.centroid,
                    kicad_mod=EXCLUDED.kicad_mod, source=EXCLUDED.source,
                    fetched_at=now()
                """,
                (
                    lcsc,
                    _jsonb_or_none(data.get("pads")),
                    _jsonb_or_none(data.get("pin_map")),
                    _jsonb_or_none(data.get("courtyard")),
                    _jsonb_or_none(data.get("centroid")),
                    data.get("kicad_mod"),
                    data.get("source"),
                ),
            )

    # -- delete ---------------------------------------------------------
    def pcb_delete(self, ref_id: int) -> dict[str, int]:
        """Soft-delete a design: mark the ref deleted, retire its graph rows,
        drop its search card — atomically."""
        counts = {}
        with self.tx() as conn:
            self.soft_delete_ref(ref_id, conn=conn)
            for tbl in (
                "pcb_instances",
                "pcb_components",
                "pcb_nets",
                "pcb_measures",
                "pcb_features",
            ):
                counts[tbl] = conn.execute(
                    f"UPDATE {tbl} SET retired_at = now() "
                    "WHERE ref_id = %s AND retired_at IS NULL",
                    (ref_id,),
                ).rowcount
            conn.execute(
                "DELETE FROM chunks WHERE ref_id = %s AND chunk_kind = 'card_combined'",
                (ref_id,),
            )
        return counts
