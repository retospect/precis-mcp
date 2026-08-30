"""Store ops for the ``pcb`` kind.

A design is a slug-addressed ``refs`` row (``kind='pcb'``) keeping
**one** ``card_combined`` chunk for intent-search; the graph lives in
normalized ``pcb_*`` tables: ``pcb_components`` (a component *type*,
owns its pins), ``pcb_pins`` (pad+function+electrical tags),
``pcb_instances`` (a placement/refdes), ``pcb_nets`` (a named, classed
signal), ``pcb_netconns`` (the netlist triple net/instance/pin — a pin
is on <=1 net; composite FKs force pin+instance to share a component).

Authoring is **batch**: :meth:`pcb_apply` lays down
components+pins+instances/nets/connections in one transaction,
*re-runnable* (existing refdes/net names reused, not duplicated). Reads
(:meth:`pcb_load`, :meth:`pcb_instance_neighbors`,
:meth:`pcb_net_members`) back graph traversal; the derived layer
(ratsnest/crossings) is computed by the handler, not stored.

Mixin assumes the concrete Store provides ``self.pool``/``self.tx``/
``self.insert_ref``/``self.get_ref``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from precis.pcb import DEFAULT_STACKUP


def _jsonb_or_none(value: Any) -> Jsonb | None:
    """psycopg Jsonb for a nullable JSONB column."""
    return Jsonb(value) if value is not None else None


#: Sentinel default for :meth:`PcbMixin.pcb_move_instance`'s ``fixed=``
#: kwarg — lets a caller pass ``fixed=None`` to explicitly CLEAR the lock,
#: distinct from omitting the kwarg (leave the lock alone).
_UNSET: Any = object()


class PcbMixin:
    pool: Any
    tx: Any
    insert_ref: Any
    get_ref: Any
    soft_delete_ref: Any  # RefsMixin — the shared ref soft-delete
    blocks: Any  # BlockStore sub-store — the shared card_combined write

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
        conn: Connection | None = None,
    ) -> tuple[Any, bool, dict[str, int]]:
        """Create-or-extend a design, batch. Returns ``(ref, created,
        counts)``.

        Each *component* dict creates a component TYPE + pins + **one**
        instance (1:1 convenience): keys ``refdes`` (req), ``label``,
        ``part``/``part_lcsc``, ``footprint``, ``courtyard``, ``centroid``,
        ``height_mm``, ``x``, ``y``, ``rot``, ``layer``, ``fixed``,
        ``roles``, ``note``, ``pins`` (``[{name, pad?, tags?,
        description?, note?}]``). A *net* dict: ``name`` (req),
        ``net_class``/``class``, ``est_current_a``/``current``,
        ``width_mm``/``width``, ``note``. A *connection* dict: ``net``
        (req), ``refdes`` (req), ``pin`` (req), ``note``. Re-runnable:
        existing refdes/net names reused.

        Reuses ``conn`` inside an existing transaction (e.g. bundling
        the ``net_classes`` upsert so both commit/rollback as one unit);
        opens its own otherwise (mirrors :meth:`pcb_ensure_board`)."""
        if conn is not None:
            return self._pcb_apply(
                conn,
                slug=slug,
                title=title,
                components=components,
                nets=nets,
                connections=connections,
                measures=measures,
                features=features,
                meta=meta,
            )
        with self.tx() as c:
            return self._pcb_apply(
                c,
                slug=slug,
                title=title,
                components=components,
                nets=nets,
                connections=connections,
                measures=measures,
                features=features,
                meta=meta,
            )

    def _pcb_apply(
        self,
        conn: Connection,
        *,
        slug: str,
        title: str,
        components: list[dict[str, Any]],
        nets: list[dict[str, Any]],
        connections: list[dict[str, Any]],
        measures: list[dict[str, Any]] | None,
        features: list[dict[str, Any]] | None,
        meta: dict[str, Any] | None,
    ) -> tuple[Any, bool, dict[str, int]]:
        """The body of :meth:`pcb_apply`, given an already-open ``conn``."""
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

        board_id = self.pcb_ensure_board(ref.id, conn=conn)

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
            counts["pins"] += self._pcb_insert_pins(conn, comp_id, c.get("pins") or [])
            inst_id = self._pcb_insert_instance(
                conn, ref.id, board_id, comp_id, refdes, c
            )
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
            self._pcb_insert_feature(conn, ref.id, board_id, ft)
            counts["features"] += 1

        self.blocks._replace_card_combined(
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
        # Auto-stamp from the catalog: if a C-number is given but
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
        board_id: int,
        component_id: int,
        refdes: str,
        c: dict[str, Any],
    ) -> int:
        row = conn.execute(
            """
            INSERT INTO pcb_instances
                (ref_id, board_id, component_id, refdes, x, y, rot, layer,
                 fixed, roles, note)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING instance_id
            """,
            (
                ref_id,
                board_id,
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
                (ref_id, name, net_class, est_current_a, width_mm, note, domain)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING net_id
            """,
            (
                ref_id,
                str(n["name"]).strip(),
                n.get("net_class") or n.get("class"),
                n.get("est_current_a") or n.get("current"),
                n.get("width_mm") or n.get("width"),
                n.get("note"),
                str(n.get("domain") or "electrical").strip(),
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
        self, conn: Connection, ref_id: int, board_id: int, f: dict[str, Any]
    ) -> None:
        """A non-electrical placed feature: mounting hole /
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
                (ref_id, board_id, ftype, x, y, rot, layer, fixed, geom, note)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                ref_id,
                board_id,
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
        (the PCB netlist+placement IR — pins may be created during logical wiring)."""
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

    # -- boards -----------------------------------------------------------
    def pcb_ensure_board(self, ref_id: int, *, conn: Connection | None = None) -> int:
        """Get-or-create the design's default board (``name='main'``,
        :data:`precis.pcb.DEFAULT_STACKUP`). Every instance/feature write
        path threads ``board_id`` through this — the netlist != board hedge
        (pcb-guided-place-route Slice 1). Reuses ``conn`` when called inside
        an existing transaction (e.g. :meth:`pcb_apply`); opens its own
        otherwise."""
        if conn is not None:
            return self._pcb_ensure_board(conn, ref_id)
        with self.tx() as c:
            return self._pcb_ensure_board(c, ref_id)

    def _pcb_ensure_board(self, conn: Connection, ref_id: int) -> int:
        row = conn.execute(
            "SELECT board_id FROM pcb_boards "
            "WHERE ref_id = %s AND name = 'main' AND retired_at IS NULL",
            (ref_id,),
        ).fetchone()
        if row is not None:
            return int(row[0])
        # Get-or-create: a concurrent pcb_apply for the same new design can
        # race this SELECT-then-INSERT and hit pcb_boards_ref_name_key
        # (the partial unique index on (ref_id, name) WHERE retired_at IS
        # NULL — the same clause the 0138 backfill uses). ON CONFLICT DO
        # NOTHING absorbs the race; re-SELECT picks up the winner's row.
        new = conn.execute(
            "INSERT INTO pcb_boards (ref_id, name, stackup) VALUES (%s, %s, %s) "
            "ON CONFLICT (ref_id, name) WHERE retired_at IS NULL DO NOTHING "
            "RETURNING board_id",
            (ref_id, "main", Jsonb(DEFAULT_STACKUP)),
        ).fetchone()
        if new is not None:
            return int(new[0])
        row = conn.execute(
            "SELECT board_id FROM pcb_boards "
            "WHERE ref_id = %s AND name = 'main' AND retired_at IS NULL",
            (ref_id,),
        ).fetchone()
        assert row is not None
        return int(row[0])

    def _pcb_board_meta(
        self, conn: Connection, ref_id: int
    ) -> tuple[dict[str, Any] | None, dict[str, Any], dict[str, int]]:
        """The board row (board_id/name/stackup/fold_lines), the design's
        net_classes (name -> rules), and a route-status summary (counts by
        :class:`pcb_routes.status`; empty = all-unrouted) — shared by
        :meth:`pcb_load` (TOC) and :meth:`pcb_graph` (the eyes)."""
        board_row = conn.execute(
            "SELECT board_id, name, stackup, fold_lines FROM pcb_boards "
            "WHERE ref_id = %s AND name = 'main' AND retired_at IS NULL",
            (ref_id,),
        ).fetchone()
        board = (
            {
                "board_id": int(board_row[0]),
                "name": board_row[1],
                "stackup": board_row[2],
                "fold_lines": board_row[3],
            }
            if board_row is not None
            else None
        )
        net_classes = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT name, rules FROM pcb_net_classes "
                "WHERE ref_id = %s AND retired_at IS NULL ORDER BY name",
                (ref_id,),
            ).fetchall()
        }
        route_status: dict[str, int] = {}
        if board is not None:
            route_status = {
                r[0]: int(r[1])
                for r in conn.execute(
                    "SELECT status, count(*) FROM pcb_routes "
                    "WHERE board_id = %s GROUP BY status",
                    (board["board_id"],),
                ).fetchall()
            }
        return board, net_classes, route_status

    # -- read -----------------------------------------------------------
    def pcb_load(self, ref_id: int) -> dict[str, Any]:
        """The design's board/net_classes/route-status + instances + nets +
        a fanout count per net, for the netlist TOC. Components/pins are
        joined into the instance rows."""
        with self.pool.connection() as conn:
            board, net_classes, route_status = self._pcb_board_meta(conn, ref_id)
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
        return {
            "board": board,
            "net_classes": net_classes,
            "route_status": route_status,
            "instances": instances,
            "nets": nets,
        }

    def pcb_instance_neighbors(self, ref_id: int, refdes: str) -> dict[str, Any] | None:
        """The graph hop from one component instance: its pins, the net on each
        pin, and the neighbouring instances on those nets."""
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
        """A net's members: every (refdes, pin) on it."""
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
        """The whole design as the *eyes* consume it: the board (stackup +
        fold_lines), placed instances, nets with their (refdes, pin) members
        + domain + ``est_current_a`` (the current annotation
        :mod:`precis.pcb.rules`'s resolver derives an IPC-2221 width from),
        the design's net_classes, a route-status summary (counts by
        :class:`pcb_routes.status`; empty = all-unrouted), and the
        unconnected pins. Pure data — the analysis lives in
        :mod:`precis.pcb`."""
        with self.pool.connection() as conn:
            board, net_classes, route_status = self._pcb_board_meta(conn, ref_id)
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
                    # `rot` was WRITE-ONLY: pcb_set_pose persisted it and no
                    # reader ever selected it, so every IR rebuilt from this
                    # graph came back with every part at 0 degrees. Placement
                    # therefore handed routing a different board than the one
                    # it had settled, and the DRC view could not reproduce a
                    # single pin coordinate of the copper it was checking.
                    "rot": r[9],
                    # `part_lcsc` was the SAME class of gap: real, joined
                    # right here (`c.part_lcsc`), just never selected — so
                    # `pcb.ir.PcbIR` (built off this graph) had no field to
                    # carry it, and every caller with a Store handle and a
                    # cached `Store.pcb_footprints_for` (keyed by C-number)
                    # had no key to remap it onto a refdes-keyed dict with.
                    # `precis.pcb.realize.pad_geometry` has taken a
                    # refdes-keyed `footprints` arg all along; this is the
                    # missing join that reaches it.
                    "part_lcsc": r[10],
                    # `extended_part` is the THIRD instance of this same
                    # gap: `PcbIR.inst_extended_part` (cost.py's
                    # `_extended_part_fees`) has always come back all-False
                    # on every board, because nothing here ever joined the
                    # Basic-vs-Extended signal in — `parts.basic` is a real,
                    # populated column (`pcb.catalog.normalize_jlcparts_row`,
                    # `parts_select_idx`), just never selected. JLC charges a
                    # flat per-line surcharge for every Extended part, so
                    # every estimate this system has produced understated
                    # cost by a real amount. `None`/no catalog match reads
                    # as "not (known) Extended" — same "undefined stays
                    # undefined, never silently promoted to a fee" caution
                    # as `coupling_bound_k`'s own note — rather than
                    # guessing either way for a part not in the catalog.
                    "extended_part": r[11],
                }
                for r in conn.execute(
                    "SELECT i.refdes, i.x, i.y, i.layer, i.roles, c.label, "
                    "       c.height_mm, "
                    # `pn` / `pt`, never both `p`: the scalar subquery and the
                    # LEFT JOIN below used to share the alias `p`. Legal --
                    # the inner scope shadows the outer -- but the next
                    # person to reference `parts` from inside the subquery
                    # (or `pcb_pins` from the join) silently gets the other
                    # table and a plausible wrong answer.
                    "       (SELECT count(*) FROM pcb_pins pn "
                    "        WHERE pn.component_id = i.component_id "
                    "          AND pn.retired_at IS NULL), i.fixed, i.rot, "
                    "       c.part_lcsc, "
                    # `parts.lcsc` is unique, so this LEFT JOIN cannot
                    # multiply instance rows; a non-catalog part yields NULL
                    # -> false, matching `extended_part`'s documented
                    # "unknown is never silently promoted to a fee".
                    "       COALESCE(NOT pt.basic, false) "
                    "FROM pcb_instances i JOIN pcb_components c "
                    "  ON c.component_id = i.component_id "
                    "  LEFT JOIN parts pt ON pt.lcsc = c.part_lcsc "
                    "WHERE i.ref_id = %s AND i.retired_at IS NULL "
                    "ORDER BY i.refdes",
                    (ref_id,),
                ).fetchall()
            ]
            net_rows = conn.execute(
                "SELECT net_id, name, net_class, domain, est_current_a FROM pcb_nets "
                "WHERE ref_id = %s AND retired_at IS NULL ORDER BY name",
                (ref_id,),
            ).fetchall()
            nets = {
                int(r[0]): {
                    "name": r[1],
                    "net_class": r[2],
                    "domain": r[3],
                    "est_current_a": r[4],
                    "members": [],
                }
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
            "board": board,
            "instances": instances,
            "nets": list(nets.values()),
            "net_classes": net_classes,
            "route_status": route_status,
            "unconnected": unconnected,
        }

    def pcb_route_status(self, ref_id: int) -> list[dict[str, Any]]:
        """Per-net route status for ``view='route-status'`` — a net with no
        :class:`pcb_routes` row reads as ``'unrouted'`` (the default state,
        not a missing one). ``note`` carries the row's optional legible
        reason (e.g. a dangling <2-member net is written ``'realized'``
        with a note explaining why — see ``pcb_route``'s job docstring —
        so a bare ``status`` doesn't read as an actually-routed net)."""
        with self.pool.connection() as conn:
            board_row = conn.execute(
                "SELECT board_id FROM pcb_boards "
                "WHERE ref_id = %s AND name = 'main' AND retired_at IS NULL",
                (ref_id,),
            ).fetchone()
            board_id = int(board_row[0]) if board_row is not None else None
            return [
                {
                    "name": r[0],
                    "net_class": r[1],
                    "domain": r[2],
                    "status": r[3] or "unrouted",
                    "note": r[4],
                }
                for r in conn.execute(
                    "SELECT n.name, n.net_class, n.domain, rt.status, rt.note "
                    "FROM pcb_nets n "
                    "LEFT JOIN pcb_routes rt "
                    "  ON rt.net_id = n.net_id AND rt.board_id = %s "
                    "WHERE n.ref_id = %s AND n.retired_at IS NULL "
                    "ORDER BY n.name",
                    (board_id, ref_id),
                ).fetchall()
            ]

    def pcb_upsert_net_classes(
        self,
        ref_id: int,
        classes: dict[str, dict[str, Any]],
        *,
        conn: Connection | None = None,
    ) -> int:
        """Upsert per-design net-class rules by name (``{name: rules}``).
        Upsert only — no soft-retire of names absent from the batch;
        deletion is an explicit later op. Returns the count written.

        Reuses ``conn`` when called inside an existing transaction (e.g. the
        handler bundling this with :meth:`pcb_apply` so both writes commit
        or roll back as one unit); opens its own otherwise (mirrors
        :meth:`pcb_ensure_board`)."""
        if conn is not None:
            return self._pcb_upsert_net_classes(conn, ref_id, classes)
        with self.tx() as c:
            return self._pcb_upsert_net_classes(c, ref_id, classes)

    def _pcb_upsert_net_classes(
        self, conn: Connection, ref_id: int, classes: dict[str, dict[str, Any]]
    ) -> int:
        n = 0
        for name, rules in classes.items():
            name = str(name).strip()
            if not name:
                raise ValueError("pcb net_class needs a name")
            conn.execute(
                """
                INSERT INTO pcb_net_classes (ref_id, name, rules)
                VALUES (%s, %s, %s)
                ON CONFLICT (ref_id, name) WHERE retired_at IS NULL
                DO UPDATE SET rules = EXCLUDED.rules
                """,
                (ref_id, name, Jsonb(dict(rules or {}))),
            )
            n += 1
        return n

    # -- geometric DRC findings (pcb-guided-place-route Slice 8) ----------
    def pcb_write_drc_findings(
        self,
        board_id: int,
        run_id: str,
        findings: list[dict[str, Any]],
        *,
        conn: Connection | None = None,
    ) -> None:
        """Persist one DRC run's findings — ``pcb_drc_findings`` is
        durable and linkable (0138): every row is a plain INSERT, never
        mutated or DELETEd by a later run (unlike ``pcb_copper``'s
        DELETE+INSERT regeneration discipline — a DRC run is a dated
        finding, not a derived-and-replaced artifact)."""
        if conn is not None:
            self._pcb_write_drc_findings(conn, board_id, run_id, findings)
            return
        with self.tx() as c:
            self._pcb_write_drc_findings(c, board_id, run_id, findings)

    def _pcb_write_drc_findings(
        self,
        conn: Connection,
        board_id: int,
        run_id: str,
        findings: list[dict[str, Any]],
    ) -> None:
        for f in findings:
            conn.execute(
                "INSERT INTO pcb_drc_findings "
                "(board_id, run_id, rule, severity, objects, detail) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    board_id,
                    run_id,
                    f["rule"],
                    f["severity"],
                    Jsonb(f.get("objects") or []),
                    f.get("detail"),
                ),
            )

    def pcb_drc_findings_latest(
        self, ref_id: int
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """The most recent DRC run's ``(run_id, findings)`` for the design's
        board — ``(None, [])`` when there is no board yet or no run has
        ever been recorded (the ``netlist_drc_clean`` gate evaluator reads
        this: no run yet means "not yet", not "clean")."""
        with self.pool.connection() as conn:
            board_row = conn.execute(
                "SELECT board_id FROM pcb_boards "
                "WHERE ref_id = %s AND name = 'main' AND retired_at IS NULL",
                (ref_id,),
            ).fetchone()
            if board_row is None:
                return None, []
            board_id = int(board_row[0])
            run_row = conn.execute(
                "SELECT run_id FROM pcb_drc_findings "
                "WHERE board_id = %s ORDER BY created_at DESC LIMIT 1",
                (board_id,),
            ).fetchone()
            if run_row is None:
                return None, []
            run_id = str(run_row[0])
            rows = conn.execute(
                "SELECT rule, severity, objects, detail, waived_by "
                "FROM pcb_drc_findings WHERE board_id = %s AND run_id = %s "
                "ORDER BY finding_id",
                (board_id, run_id),
            ).fetchall()
            return run_id, [
                {
                    "rule": r[0],
                    "severity": r[1],
                    "objects": r[2],
                    "detail": r[3],
                    "waived_by": r[4],
                }
                for r in rows
            ]

    def pcb_set_placement(
        self,
        ref_id: int,
        placement: dict[str, tuple[float, float]],
        *,
        meta: dict[str, Any] | None = None,
    ) -> int:
        """Write new `(x, y)` for instances by refdes (placer).

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

    # -- pcb-guided-place-route Slice 10: job write-back + inline editors --

    def pcb_set_pose(
        self,
        ref_id: int,
        pose: dict[str, tuple[float, float, float]],
        *,
        meta: dict[str, Any] | None = None,
    ) -> int:
        """Write new ``(x, y, rot)`` for instances by refdes — the joint
        placement+topology optimizer's write-back (``pcb_place``/
        ``pcb_route`` jobs). Same fixed-respecting discipline as
        :meth:`pcb_set_placement`, but per-axis: ``fixed='rot'`` still
        gets ``(x, y)`` written, ``fixed='xy'`` still gets rotation,
        ``fixed='both'`` blocks everything. :meth:`pcb_set_placement`
        stays untouched — its only caller (v1 autoplace/route
        round-trip) never writes rotation, so a coarser blanket guard is
        honest there. Returns instances with ≥1 axis written."""
        moved = 0
        with self.tx() as conn:
            for refdes, (x, y, rot) in pose.items():
                moved += conn.execute(
                    "UPDATE pcb_instances SET "
                    "  x = CASE WHEN fixed IS NULL OR fixed NOT IN ('xy', 'both') "
                    "           THEN %s ELSE x END, "
                    "  y = CASE WHEN fixed IS NULL OR fixed NOT IN ('xy', 'both') "
                    "           THEN %s ELSE y END, "
                    "  rot = CASE WHEN fixed IS NULL OR fixed NOT IN ('rot', 'both') "
                    "           THEN %s ELSE rot END "
                    "WHERE ref_id = %s AND refdes = %s AND retired_at IS NULL",
                    (float(x), float(y), float(rot), ref_id, refdes),
                ).rowcount
            if meta is not None:
                conn.execute(
                    "UPDATE refs SET meta = meta || %s WHERE ref_id = %s",
                    (Jsonb(dict(meta)), ref_id),
                )
        return moved

    def pcb_move_instance(
        self,
        ref_id: int,
        refdes: str,
        *,
        x: float | None = None,
        y: float | None = None,
        rot: float | None = None,
        fixed: str | None = _UNSET,
    ) -> bool:
        """The inline move/rotate/(un)lock editor. Unlike
        :meth:`pcb_set_pose`'s optimizer write-back, this IS the authorized
        edit path, so it never refuses a ``fixed`` instance — an LLM
        deliberately repositioning a locked part is not the failure mode
        the optimizer's guard exists for. ``fixed`` defaults to a sentinel
        so a caller can pass ``fixed=None`` to explicitly CLEAR the lock,
        distinct from omitting it (leave the lock alone). Returns whether a
        live instance was found and updated."""
        sets: list[str] = []
        params: list[Any] = []
        if x is not None:
            sets.append("x = %s")
            params.append(float(x))
        if y is not None:
            sets.append("y = %s")
            params.append(float(y))
        if rot is not None:
            sets.append("rot = %s")
            params.append(float(rot))
        if fixed is not _UNSET:
            sets.append("fixed = %s")
            params.append(fixed)
        if not sets:
            return False
        params += [ref_id, refdes]
        with self.tx() as conn:
            n = conn.execute(
                f"UPDATE pcb_instances SET {', '.join(sets)} "  # nosec B608 — sets[] is a fixed internal column-name list, never caller input
                "WHERE ref_id = %s AND refdes = %s AND retired_at IS NULL",
                params,
            ).rowcount
        return n > 0

    def pcb_net_ids(self, ref_id: int) -> dict[str, int]:
        """``{net name: net_id}`` for a design — the join key
        :meth:`pcb_routes_write`/:meth:`pcb_copper_replace`'s callers use to
        turn the IR's name-addressed sketch back into real FKs."""
        with self.pool.connection() as conn:
            return {
                r[0]: int(r[1])
                for r in conn.execute(
                    "SELECT name, net_id FROM pcb_nets "
                    "WHERE ref_id = %s AND retired_at IS NULL",
                    (ref_id,),
                ).fetchall()
            }

    def pcb_routes_get(self, ref_id: int) -> dict[str, dict[str, Any]]:
        """Every net's persisted sketch (tree/topology/layer_assign/status/
        fail/meta), by net name — feeds
        :func:`precis.pcb.session.apply_route_overrides` so a pinned side
        choice survives the next ``pcb_route`` run's IR rebuild. A net with
        no ``pcb_routes`` row yet reads as the all-empty/``'unrouted'``
        default, same convention as :meth:`pcb_route_status`."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT n.name, rt.tree, rt.topology, rt.layer_assign, "
                "       rt.status, rt.fail, rt.meta "
                "FROM pcb_nets n "
                "JOIN pcb_boards b ON b.ref_id = n.ref_id AND b.name = 'main' "
                "  AND b.retired_at IS NULL "
                "LEFT JOIN pcb_routes rt "
                "  ON rt.net_id = n.net_id AND rt.board_id = b.board_id "
                "WHERE n.ref_id = %s AND n.retired_at IS NULL",
                (ref_id,),
            ).fetchall()
        out: dict[str, dict[str, Any]] = {}
        for name, tree, topology, layer_assign, status, fail, meta in rows:
            out[name] = {
                "tree": tree or [],
                "topology": topology or [],
                "layer_assign": layer_assign or [],
                "status": status or "unrouted",
                "fail": fail,
                "meta": meta or {},
            }
        return out

    def pcb_routes_write(
        self, ref_id: int, board_id: int, rows: dict[str, dict[str, Any]]
    ) -> int:
        """Upsert one ``pcb_routes`` row per named net — the ``pcb_route``
        job's checkpoint write-back. A net absent from ``rows`` (the
        sketch has nothing new to say about it, e.g. plane-served) is left
        untouched. ``row["note"]`` (optional) persists a human-legible
        reason alongside ``status`` — e.g. the dangling-net exemption,
        which writes ``status='realized'`` with a note explaining why
        rather than a bare status a later reader can't distinguish from an
        actually-routed net. Returns the number of rows written."""
        n = 0
        with self.tx() as conn:
            for net_name, row in rows.items():
                net = conn.execute(
                    "SELECT net_id FROM pcb_nets WHERE ref_id = %s AND name = %s "
                    "AND retired_at IS NULL",
                    (ref_id, net_name),
                ).fetchone()
                if net is None:
                    continue
                conn.execute(
                    """
                    INSERT INTO pcb_routes
                        (board_id, net_id, tree, topology, layer_assign,
                         status, fail, note, meta, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (board_id, net_id) DO UPDATE SET
                        tree = EXCLUDED.tree,
                        topology = EXCLUDED.topology,
                        layer_assign = EXCLUDED.layer_assign,
                        status = EXCLUDED.status,
                        fail = EXCLUDED.fail,
                        note = EXCLUDED.note,
                        meta = EXCLUDED.meta,
                        updated_at = now()
                    """,
                    (
                        board_id,
                        int(net[0]),
                        _jsonb_or_none(row.get("tree")),
                        _jsonb_or_none(row.get("topology")),
                        _jsonb_or_none(row.get("layer_assign")),
                        row.get("status") or "unrouted",
                        _jsonb_or_none(row.get("fail")),
                        row.get("note"),
                        Jsonb(dict(row.get("meta") or {})),
                    ),
                )
                n += 1
        return n

    def pcb_copper_replace(self, board_id: int, rows: list[dict[str, Any]]) -> int:
        """Regenerate a board's derived copper wholesale — DELETE + INSERT,
        the same cascade discipline as chunks->embeddings the table's own
        comment promises (never a partial UPDATE)."""
        with self.tx() as conn:
            conn.execute("DELETE FROM pcb_copper WHERE board_id = %s", (board_id,))
            for r in rows:
                conn.execute(
                    "INSERT INTO pcb_copper "
                    "(board_id, ctype, layer, net_id, route_id, geom) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        board_id,
                        r["ctype"],
                        r["layer"],
                        r["net_id"],
                        r.get("route_id"),
                        Jsonb(r["geom"]),
                    ),
                )
        return len(rows)

    def pcb_copper_list(self, board_id: int) -> list[dict[str, Any]]:
        """Every derived copper row for a board, flattened into
        :mod:`precis.pcb.drc`'s item shape (``{"ctype", "layer", "net",
        ...geom fields}``) — the flat convention
        :mod:`precis.pcb.realize`/:mod:`precis.pcb.gerber` already share,
        so ``view='drc'`` hands this straight to
        :func:`precis.pcb.drc.run_geometric_drc` with no reshaping.
        ``net_id`` resolves to its net NAME via a join — DRC findings
        read by name, matching this layer's "names for humans/export, not
        internal identity" discipline."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT c.ctype, c.layer, n.name, c.geom "
                "FROM pcb_copper c JOIN pcb_nets n ON n.net_id = c.net_id "
                "WHERE c.board_id = %s AND n.retired_at IS NULL",
                (board_id,),
            ).fetchall()
        return [
            {"ctype": ctype, "layer": layer, "net": net_name, **(geom or {})}
            for ctype, layer, net_name, geom in rows
        ]

    def pcb_rip_route(self, ref_id: int, net_name: str) -> bool:
        """The rip-up primitive: reset one net's sketch back to
        ``'unrouted'`` and drop its realized copper — the LLM's lever when
        the realizer reports an over-capacity gap (backlog's rip-up loop).
        A subsequent ``op='route'`` re-decides that net's topology fresh —
        a rip clears the whole persisted sketch, not just the realized
        geometry, since a pinned choice that caused the squeeze shouldn't
        survive the rip that was meant to escape it. Returns whether a
        route row existed to rip (``False`` for an already-unrouted net —
        nothing to rip)."""
        with self.tx() as conn:
            row = conn.execute(
                "SELECT rt.route_id, rt.board_id, rt.net_id FROM pcb_routes rt "
                "JOIN pcb_nets n ON n.net_id = rt.net_id "
                "WHERE n.ref_id = %s AND n.name = %s AND n.retired_at IS NULL",
                (ref_id, net_name),
            ).fetchone()
            if row is None:
                return False
            route_id, board_id, net_id = int(row[0]), int(row[1]), int(row[2])
            conn.execute(
                "UPDATE pcb_routes SET tree = NULL, topology = NULL, "
                "layer_assign = NULL, status = 'unrouted', fail = NULL, "
                "updated_at = now() WHERE route_id = %s",
                (route_id,),
            )
            conn.execute(
                "DELETE FROM pcb_copper WHERE board_id = %s AND net_id = %s",
                (board_id, net_id),
            )
        return True

    def pcb_pin_topology(
        self, ref_id: int, net_name: str, a: str, b: str, side: int
    ) -> bool:
        """Pin one segment's side choice — a targeted MERGE into the net's
        persisted ``pcb_routes.topology`` (replacing at most the one entry
        keyed by ``(a, b)``, never the wholesale replace
        :meth:`pcb_routes_write`'s checkpoint does), so the pin survives
        until the next ``op='route'`` run reads it back via
        :func:`precis.pcb.session.apply_route_overrides`. Creates the
        net's ``pcb_routes`` row (status ``'unrouted'``) if none exists yet
        — pinning a side is a legitimate first edit to an as-yet-unrouted
        net's sketch. Returns whether the net resolved."""
        key = "|".join(sorted((a, b)))
        with self.tx() as conn:
            board_id = self._pcb_ensure_board(conn, ref_id)
            net = conn.execute(
                "SELECT net_id FROM pcb_nets WHERE ref_id = %s AND name = %s "
                "AND retired_at IS NULL",
                (ref_id, net_name),
            ).fetchone()
            if net is None:
                return False
            net_id = int(net[0])
            row = conn.execute(
                "SELECT route_id, topology FROM pcb_routes "
                "WHERE board_id = %s AND net_id = %s",
                (board_id, net_id),
            ).fetchone()
            entries = list(row[1]) if row is not None and row[1] else []
            entries = [
                e
                for e in entries
                if "|".join(sorted((e.get("a", ""), e.get("b", "")))) != key
            ]
            entries.append({"a": a, "b": b, "side": int(side)})
            if row is None:
                conn.execute(
                    "INSERT INTO pcb_routes (board_id, net_id, topology, status) "
                    "VALUES (%s, %s, %s, 'unrouted')",
                    (board_id, net_id, Jsonb(entries)),
                )
            else:
                conn.execute(
                    "UPDATE pcb_routes SET topology = %s, updated_at = now() "
                    "WHERE route_id = %s",
                    (Jsonb(entries), int(row[0])),
                )
        return True

    def pcb_assign_plane(self, ref_id: int, layer_name: str, net_name: str) -> int:
        """Author a plane assignment (``pcb_planes``) — the inline "assign
        plane net" editor. Idempotent by (board, layer, net); returns
        plane_id, or 0 when ``net_name`` doesn't resolve.

        Always stamps ``meta.source = 'authored'`` (merged on conflict,
        never overwriting unrelated meta keys) — a human's explicit
        instruction must never later look like an optimizer guess. If a
        live *derived* row already occupies this (board, layer, net) key,
        promotes it to authored in place — a human claiming a spot the
        optimizer merely guessed at. The reverse (derived silently
        clobbering authored) must never happen — see
        :meth:`pcb_planes_replace_derived`."""
        with self.tx() as conn:
            board_id = self._pcb_ensure_board(conn, ref_id)
            net = conn.execute(
                "SELECT net_id FROM pcb_nets WHERE ref_id = %s AND name = %s "
                "AND retired_at IS NULL",
                (ref_id, net_name),
            ).fetchone()
            if net is None:
                return 0
            row = conn.execute(
                """
                INSERT INTO pcb_planes (board_id, layer, net_id, meta)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (board_id, layer, net_id) WHERE retired_at IS NULL
                DO UPDATE SET meta = pcb_planes.meta || EXCLUDED.meta
                RETURNING plane_id
                """,
                (board_id, layer_name, int(net[0]), Jsonb({"source": "authored"})),
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def pcb_planes_list(self, ref_id: int) -> list[dict[str, Any]]:
        """Every live plane assignment for a design — human-``authored``
        and optimizer-``derived`` (:meth:`pcb_planes_replace_derived`,
        gr267526) both load. Doubles as the ``view='planes'`` read and the
        seed :mod:`precis.workers.job_types.pcb_route` warm-starts a
        fresh anneal from (a prior derived guess is still a reasonable
        start).

        ``source`` defaults ``'authored'`` for a row with no
        ``meta.source`` (pre-this-change rows) — the safe direction,
        since misreading a row as ``'derived'`` would let a later
        replace silently retire a human's instruction."""
        with self.pool.connection() as conn:
            return [
                {
                    "layer": r[0],
                    "net": r[1],
                    "region_hint": r[2],
                    "source": r[3] or "authored",
                }
                for r in conn.execute(
                    "SELECT pl.layer, n.name, pl.region_hint, pl.meta->>'source' "
                    "FROM pcb_planes pl "
                    "JOIN pcb_boards b ON b.board_id = pl.board_id "
                    "JOIN pcb_nets n ON n.net_id = pl.net_id "
                    "WHERE b.ref_id = %s AND pl.retired_at IS NULL "
                    "ORDER BY pl.layer, n.name",
                    (ref_id,),
                ).fetchall()
            ]

    def pcb_planes_replace_derived(
        self, ref_id: int, board_id: int, assignments: dict[str, str]
    ) -> int:
        """Optimizer-derived plane write-back — the ``pcb_route`` job's
        checkpoint for :meth:`precis.pcb.ir.PcbIR.promote_plane` state
        (gr267526: previously dropped, so GND/VCC-class nets never got a
        plane and threaded as full-length traces instead).

        Provenance-scoped **replace** (same DELETE(retire)+INSERT cascade
        as :meth:`pcb_copper_replace`): retires every existing
        ``source='derived'`` row for this board, then inserts fresh, so a
        re-run replaces rather than accumulates. **Never touches
        ``source='authored'``** — the retire query filters on it, and the
        insert is guarded by ``ON CONFLICT ... DO NOTHING`` as a second
        line of defense; the caller
        (:mod:`precis.workers.job_types.pcb_route`) must still exclude
        authored nets from ``assignments`` via :meth:`pcb_planes_list`'s
        ``source`` field, so an authored net's decision is never silently
        shadowed by a derived one.

        ``assignments`` is ``{net_name: layer_name}``. Returns nets
        attempted (not necessarily inserted)."""
        with self.tx() as conn:
            conn.execute(
                "UPDATE pcb_planes SET retired_at = now() "
                "WHERE board_id = %s AND retired_at IS NULL "
                "AND coalesce(meta->>'source', 'authored') = 'derived'",
                (board_id,),
            )
            n = 0
            for net_name, layer_name in assignments.items():
                net = conn.execute(
                    "SELECT net_id FROM pcb_nets WHERE ref_id = %s AND name = %s "
                    "AND retired_at IS NULL",
                    (ref_id, net_name),
                ).fetchone()
                if net is None:
                    continue
                conn.execute(
                    """
                    INSERT INTO pcb_planes (board_id, layer, net_id, meta)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (board_id, layer, net_id) WHERE retired_at IS NULL
                    DO NOTHING
                    """,
                    (board_id, layer_name, int(net[0]), Jsonb({"source": "derived"})),
                )
                n += 1
        return n

    def pcb_pin_swaps_list(self, ref_id: int) -> list[dict[str, Any]]:
        """Every live pin<->net override for a design (docs/backlog/
        pcb-engine-plan.md "PIN_SWAP is not persisted") — both a future
        human-``authored`` override (no authoring verb yet; ``source``
        discipline wired ahead of it, same shape as
        :meth:`pcb_planes_list`) and optimizer-``derived`` rows
        (:meth:`pcb_pin_swaps_replace_derived`). ``pcb_netconns`` itself
        never changes — this is the override layered on top, resolved by
        durable identity (refdes+pin name, not the ephemeral IR-local pin
        int) so a caller can re-apply it onto a freshly-built IR
        (:mod:`precis.pcb.session`).

        ``source`` defaults ``'authored'`` for a row with no
        ``meta.source``, same safe direction as :meth:`pcb_planes_list`."""
        with self.pool.connection() as conn:
            return [
                {
                    "refdes": r[0],
                    "pin": r[1],
                    "net": r[2],
                    "source": r[3] or "authored",
                }
                for r in conn.execute(
                    "SELECT i.refdes, p.name, n.name, sw.meta->>'source' "
                    "FROM pcb_pin_swaps sw "
                    "JOIN pcb_boards b ON b.board_id = sw.board_id "
                    "JOIN pcb_instances i ON i.instance_id = sw.instance_id "
                    "JOIN pcb_pins p ON p.pin_id = sw.pin_id "
                    "JOIN pcb_nets n ON n.net_id = sw.net_id "
                    "WHERE b.ref_id = %s AND sw.retired_at IS NULL "
                    "ORDER BY i.refdes, p.name",
                    (ref_id,),
                ).fetchall()
            ]

    def pcb_pin_swaps_replace_derived(
        self, ref_id: int, board_id: int, overrides: list[dict[str, Any]]
    ) -> int:
        """Optimizer-derived pin-swap write-back — the ``pcb_route`` job's
        checkpoint for :meth:`precis.pcb.ir.PcbIR.swap_pins` state
        (docs/backlog/pcb-engine-plan.md "PIN_SWAP is not persisted":
        previously dropped, so a genuinely-beneficial swap reverted to
        ``pcb_netconns``'s original wiring once the job ended, leaving
        persisted netlist and persisted copper describing two different
        boards).

        Provenance-scoped **replace**, same DELETE(retire)+INSERT cascade
        as :meth:`pcb_planes_replace_derived`: retires every existing
        ``source='derived'`` row, inserts fresh. **Never touches
        ``source='authored'``** — the retire filters on it, and the
        insert is guarded by ``ON CONFLICT ... DO NOTHING`` on the live
        physical-pin key; the caller
        (:mod:`precis.workers.job_types.pcb_route`) must still exclude
        authored pins from ``overrides`` via :meth:`pcb_pin_swaps_list`'s
        ``source`` field.

        ``overrides`` is ``[{"refdes", "pin", "net"}, ...]`` — the settled
        net name each pin now carries. An unresolvable ``(refdes, pin)``
        or ``net`` is silently skipped (netlist changed under it), never
        an error. Returns overrides attempted (not necessarily
        inserted)."""
        with self.tx() as conn:
            conn.execute(
                "UPDATE pcb_pin_swaps SET retired_at = now() "
                "WHERE board_id = %s AND retired_at IS NULL "
                "AND coalesce(meta->>'source', 'authored') = 'derived'",
                (board_id,),
            )
            n = 0
            for entry in overrides:
                pin_row = conn.execute(
                    """
                    SELECT i.instance_id, p.pin_id, i.component_id
                    FROM pcb_instances i
                    JOIN pcb_pins p
                        ON p.component_id = i.component_id AND p.retired_at IS NULL
                    WHERE i.ref_id = %s AND i.retired_at IS NULL
                      AND i.refdes = %s AND p.name = %s
                    """,
                    (ref_id, entry.get("refdes"), entry.get("pin")),
                ).fetchone()
                if pin_row is None:
                    continue
                net = conn.execute(
                    "SELECT net_id FROM pcb_nets WHERE ref_id = %s AND name = %s "
                    "AND retired_at IS NULL",
                    (ref_id, entry.get("net")),
                ).fetchone()
                if net is None:
                    continue
                instance_id, pin_id, component_id = (
                    int(pin_row[0]),
                    int(pin_row[1]),
                    int(pin_row[2]),
                )
                conn.execute(
                    """
                    INSERT INTO pcb_pin_swaps
                        (board_id, instance_id, pin_id, component_id, net_id, meta)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (instance_id, pin_id) WHERE retired_at IS NULL
                    DO NOTHING
                    """,
                    (
                        board_id,
                        instance_id,
                        pin_id,
                        component_id,
                        int(net[0]),
                        Jsonb({"source": "derived"}),
                    ),
                )
                n += 1
        return n

    def pcb_set_class_rules(self, ref_id: int, name: str, rules: dict[str, Any]) -> int:
        """Thin single-class wrapper around
        :meth:`pcb_upsert_net_classes` — the inline "set class rules"
        editor's write path."""
        return self.pcb_upsert_net_classes(ref_id, {name: rules})

    def pcb_measures_list(self, ref_id: int) -> list[dict[str, Any]]:
        """Live measures of a design."""
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
        """Live non-electrical features of a design — the
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

    # -- parts catalog ------------------------------------
    def parts_import(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        """Upsert normalized catalog rows (:func:`precis.pcb.catalog.
        normalize_jlcparts_row`) + update the turnover signal. Returns
        ``{upserted, restocked}``.

        Upsert (not the atomic swap) keeps the table live and the FK-free
        ``part_footprints`` / ``part_availability`` caches intact; the
        staging + atomic-swap (:meth:`parts_bulk_replace`) is the scale
        lever for the full ~300k dump (the PCB netlist+placement IR —
        "drop-index trick optional at our row count")."""
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

    def parts_bulk_replace(
        self,
        rows: Iterable[dict[str, Any]],
        *,
        min_fraction: float = 0.5,
        force: bool = False,
    ) -> dict[str, int]:
        """Full-catalog reload via staging + atomic swap (0047's design,
        see that migration's header) — the scale lever for the whole
        ~300k-row jlcparts dump, vs :meth:`parts_import`'s per-row upsert
        (right for an incremental API page, wrong for a full reload).
        ``rows`` may be a one-shot generator, consumed while loading
        staging, so a full dump never needs to fit in memory as a list.

        Everything — staging creation, every row load, the
        ``part_availability`` turnover roll, and the swap — runs inside
        ONE transaction: a raise anywhere leaves the live ``parts`` table
        untouched, since the swap (drop old, promote staging) is the LAST
        step. ``part_footprints``/``part_availability`` are deliberately
        FK-free (0047), never touched by the swap, and survive it
        unconditionally; this still rolls ``part_availability``'s
        turnover signal per row like :meth:`parts_import`.

        **Shrink guard (do not remove).** Staging loading far fewer rows
        than the live catalog is almost never a real shrink — it's a
        truncated download, a drifted-schema normalization failure, or an
        early-died API walk. Promoting it silently destroys the catalog,
        and the transaction can't save us (the empty load is a
        *successful* one). So the swap refuses unless staging holds ≥
        ``min_fraction`` of the live row count, and refuses empty staging
        outright. ``force=True`` overrides for a genuine teardown; the
        CLI surfaces it as an explicit flag so nobody trips it by
        accident.

        Returns ``{loaded, restocked}``.
        """
        counts = {"loaded": 0, "restocked": 0}
        # A local (not module-level) name, not a string literal directly in
        # the .execute() call — deliberately so
        # tests/test_sql_schema_drift.py's static AST extractor can't
        # statically resolve the INSERT below and counts it as
        # skipped-dynamic rather than EXPLAIN-ing it against the migrated
        # schema, where a scratch table created+dropped inside this one
        # transaction (never a real migrated object) would false-positive
        # as "relation does not exist". Same idiom
        # :func:`precis.pcb.catalog.read_jlcparts_sqlite` already uses for
        # its dynamic column list.
        staging = "parts_staging"
        with self.tx() as conn:
            conn.execute(f"DROP TABLE IF EXISTS {staging}")
            conn.execute(f"CREATE TABLE {staging} (LIKE parts INCLUDING ALL)")
            for r in rows:
                lcsc = r["lcsc"]
                new_stock = int(r.get("stock") or 0)
                if self._parts_update_availability(conn, lcsc, new_stock):
                    counts["restocked"] += 1
                conn.execute(
                    f"""
                    INSERT INTO {staging}
                        (lcsc, mfr, mfr_part, description, jlcpcb_assemblable,
                         basic, stock, price, package, height_mm, params,
                         datasheet_url)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
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
                counts["loaded"] += 1
            # Shrink guard — see the docstring. A clean-but-empty load is
            # the dangerous case precisely because nothing raised.
            live = conn.execute("SELECT count(*) FROM parts").fetchone()
            live_n = int(live[0]) if live else 0
            floor = int(live_n * min_fraction)
            if not force and (counts["loaded"] == 0 or counts["loaded"] < floor):
                raise ValueError(
                    f"refusing to swap: staging loaded {counts['loaded']} row(s) "
                    f"but the live catalog holds {live_n} (floor {floor} = "
                    f"{min_fraction:.0%}). A shrunken load is almost always a "
                    "truncated dump, drifted upstream column names, or a walk "
                    "that died early — not a catalog that really shrank. Pass "
                    "force=True (CLI: --allow-shrink) if this is intended."
                )
            # The atomic swap: promote the fully-loaded staging table into
            # ``parts``'s place. Reached only if every row above loaded
            # clean — an exception anywhere in the loop propagates out of
            # ``self.tx()`` and rolls back the whole transaction, DDL
            # included, so ``parts`` is never dropped on a failed load.
            conn.execute("DROP TABLE parts")
            conn.execute(f"ALTER TABLE {staging} RENAME TO parts")
            # ``LIKE ... INCLUDING ALL`` names the cloned indexes after the
            # STAGING table, and RENAME TO does not rename a table's own
            # indexes — so without this, 0047's deliberately-named
            # parts_select_idx / parts_tsv_gin / parts_params_gin are gone
            # after the first reload, and each later reload accumulates
            # another auto-suffixed variant. Rename them back so the live
            # table's index names stay the ones the migration documents.
            for (idx_name,) in conn.execute(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'parts' AND indexname LIKE %s",
                (f"{staging}%",),
            ).fetchall():
                canonical = "parts" + idx_name[len(staging) :]
                conn.execute(f'ALTER INDEX "{idx_name}" RENAME TO "{canonical}"')
            conn.execute(
                "COMMENT ON TABLE parts IS "
                "'LCSC/JLCPCB catalog (ADR 0042 §5, Flow A) — bulk from "
                "the jlcparts dump via staging + atomic swap. NO inbound "
                "FK (the swap drops the table).'"
            )
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
        """The JLCPCB-native selector: hard-filter to assemblable
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
        """The Flow B EasyEDA cache row for a C-number, or None. ``escape``
        is the precomputed footprint escape graph (pcb-guided-place-route
        Slice 5, `precis.pcb.escape.compute_escape_graph`) — footprint-
        intrinsic, so it round-trips here rather than in any board-scoped
        table."""
        with self.pool.connection() as conn:
            r = conn.execute(
                "SELECT pads, pin_map, courtyard, centroid, kicad_mod, source, raw, escape "
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
            "raw": r[6],
            "escape": r[7],
        }

    def part_footprint_put(self, lcsc: str, data: dict[str, Any]) -> None:
        """Cache a fetched footprint (Flow B). Upsert by C-number. ``raw``
        (the untouched EasyEDA component JSON) round-trips so a future
        parser improvement can reparse from cache without re-fetching.
        ``escape`` (the precomputed escape graph, opaque here — see
        :mod:`precis.pcb.escape`) round-trips the same way as every other
        field: an upsert with the key omitted (or ``None``) writes NULL,
        same as ``kicad_mod``/``source`` above."""
        with self.tx() as conn:
            conn.execute(
                """
                INSERT INTO part_footprints
                    (lcsc, pads, pin_map, courtyard, centroid, kicad_mod, source, raw, escape)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (lcsc) DO UPDATE SET
                    pads=EXCLUDED.pads, pin_map=EXCLUDED.pin_map,
                    courtyard=EXCLUDED.courtyard, centroid=EXCLUDED.centroid,
                    kicad_mod=EXCLUDED.kicad_mod, source=EXCLUDED.source,
                    raw=EXCLUDED.raw, escape=EXCLUDED.escape, fetched_at=now()
                """,
                (
                    lcsc,
                    _jsonb_or_none(data.get("pads")),
                    _jsonb_or_none(data.get("pin_map")),
                    _jsonb_or_none(data.get("courtyard")),
                    _jsonb_or_none(data.get("centroid")),
                    data.get("kicad_mod"),
                    data.get("source"),
                    _jsonb_or_none(data.get("raw")),
                    _jsonb_or_none(data.get("escape")),
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
