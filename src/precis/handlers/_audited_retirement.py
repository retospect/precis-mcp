"""Audited retirement shared by memory and finding records.

Keep mention guards, successor validation and link migration in one transaction
so a finding tombstone has the same repair contract as a memory tombstone.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from psycopg import Connection

from precis.errors import BadInput, Gone, NotFound
from precis.handlers._numeric_ref import NumericRefHandler
from precis.response import Response
from precis.utils import handle_registry
from precis.utils.link_target import parse_link_target


class AuditedRetirementHandler(NumericRefHandler):
    """Numeric records with prose-audited retirement and successor redirects."""

    def delete(
        self,
        *,
        id: str | int | None = None,
        reason: str | None = None,
        dry_run: bool = False,
        superseded_by: str | None = None,
        force: bool = False,
        **_kw: Any,
    ) -> Response:
        """Preview or retire with an explicit disposition for live prose mentions."""
        return self._delete(
            id, reason=reason, dry_run=dry_run, superseded_by=superseded_by, force=force
        )

    def _retire_successor(
        self, raw: str, ref_id: int, *, conn: Connection
    ) -> tuple[str, int | None, str]:
        """Validate a replacement; file- and index-backed successors have no ref."""
        if raw.startswith("md:"):
            address = raw.removeprefix("md:")
            if "~" not in address or not address.rsplit("~", 1)[1]:
                raise BadInput(
                    "an md successor requires a docs path and section",
                    next="superseded_by='md:main/docs/<file>.md~<heading>'",
                )
            handler = self.hub.handler_for("md")
            if handler is None:
                raise BadInput(
                    "md successor validation requires the md handler",
                    next="use a live record handle or enable the md reader",
                )
            handler.get(id=address)
            return raw, None, "md"
        if raw.startswith("python:"):
            address = raw.removeprefix("python:")
            if "::" not in address and "/" not in address:
                raise BadInput(
                    "a python successor requires a symbol or file address",
                    next="superseded_by='python:<alias>::<qualname>' or "
                    "'python:<alias>/<path>.py'",
                )
            handler = self.hub.handler_for("python")
            if handler is None:
                raise BadInput(
                    "python successor validation requires the python handler",
                    next="use a live record handle or configure python roots",
                )
            handler.get(id=address)
            return raw, None, "python"
        if raw.isdecimal():
            raw = handle_registry.format_handle(self.kind, int(raw))
        try:
            target = parse_link_target(raw, store=self.store)
        except NotFound as exc:
            raise BadInput(
                f"superseded_by must name a live successor: {raw}",
                next=f"get(id={raw!r})",
            ) from exc
        if target.ref_id == ref_id:
            raise BadInput(
                "a record cannot supersede itself",
                next="superseded_by='<different live handle>'",
            )
        row = conn.execute(
            "SELECT kind FROM refs WHERE ref_id = %s AND retired_at IS NULL FOR SHARE",
            (target.ref_id,),
        ).fetchone()
        if row is None:
            raise BadInput(
                "superseded_by must name a live successor",
                next=f"get(id={raw!r})",
            )
        formatted = handle_registry.try_format(str(row[0]), target.ref_id)
        if formatted is None:
            raise BadInput(
                f"superseded_by kind {row[0]!r} has no record handle",
                next="use a live record handle or an md:path~section successor",
            )
        address = formatted
        if target.pos is not None:
            # The garden's successor resolver follows records and md sections,
            # not chunks. Refuse rather than store an address it would lose.
            raise BadInput(
                "superseded_by cannot name a chunk",
                next=f"use the record handle {address!r} or an md:path~section successor",
            )
        return address, target.ref_id, str(row[0])

    def _delete_successor(
        self,
        ref_id: int,
        meta: dict[str, Any],
        *,
        conn: Any,
        superseded_by: str | None = None,
        reason: str | None = None,
    ) -> tuple[str | None, int | None, str | None]:
        """Resolve the delete route before mutation so garden can snapshot it."""
        raw_sup = superseded_by or meta.get("superseded_by")
        if superseded_by is None:
            inbound: list[str] = []
            for rid, kind in conn.execute(
                "SELECT DISTINCT r.ref_id, r.kind FROM links l JOIN refs r "
                "ON r.ref_id = l.src_ref_id WHERE l.dst_ref_id = %s "
                "AND l.relation = 'supersedes' AND r.retired_at IS NULL "
                "AND r.ref_id <> %s ORDER BY r.ref_id",
                (ref_id, ref_id),
            ):
                address = handle_registry.try_format(str(kind), int(rid))
                if address is not None:
                    inbound.append(address)
                    if len(inbound) == 2:
                        break
            if len(inbound) > 1:
                raise BadInput(
                    "multiple live successors supersede this record; choose one",
                    next=[
                        f"delete(kind={self.kind!r}, id={ref_id}, superseded_by={address!r})"
                        for address in inbound
                    ],
                )
            if raw_sup is None and inbound:
                raw_sup = inbound[0]
        if raw_sup is None and reason:
            named = self._reason_named_ref({"reason": reason})
            if (
                named is not None
                and named != ref_id
                and self.store.get_ref(kind=self.kind, id=named, conn=conn) is not None
            ):
                raw_sup = handle_registry.format_handle(self.kind, named)
        if raw_sup is None:
            return None, None, None
        return self._retire_successor(str(raw_sup), ref_id, conn=conn)

    def _delete(
        self,
        id: str | int | None,
        *,
        reason: str | None = None,
        dry_run: bool = False,
        superseded_by: str | None = None,
        force: bool = False,
    ) -> Response:
        """Scan, guard, record the redirect and retire atomically; preview writes nothing."""
        from precis.handlers._memory_retire import scan_mentions

        if id is None:
            raise BadInput(
                "delete requires id=",
                next=f"delete(kind={self.kind!r}, id=N)",
            )
        ref_id = self._coerce_id(id)
        self._resolve_ref_for_delete(ref_id)
        # Mutation review (gr626866): walk the rely-side graph before the
        # retire tx severs/migrates its edges.
        deps = self.store.dependants(ref_id, limit=10)
        from precis.handlers._dependants_view import (
            dependants_block,
            dependants_summary,
        )

        with self.store.tx() as conn:
            row = conn.execute(
                "SELECT retired_at, meta FROM refs WHERE ref_id = %s "
                "AND kind = %s FOR NO KEY UPDATE",
                (ref_id, self.kind),
            ).fetchone()
            if row is None or row[0] is not None:
                raise Gone(
                    f"{self.kind} {ref_id} is no longer live",
                    next=f"get(kind={self.kind!r}, id={ref_id}, view='retired')",
                )
            successor_handle, successor, successor_kind = self._delete_successor(
                ref_id,
                row[1] or {},
                conn=conn,
                superseded_by=superseded_by,
                reason=reason,
            )
            report = scan_mentions(conn, ref_id, kind=self.kind)
            receipt = report.render(
                handle_registry.format_handle(self.kind, ref_id),
                datetime.now(UTC).date().isoformat() + " UTC",
                successor_handle or reason or "retired without a successor",
            )
            blocked = report.blocking and successor_handle is None and not force
            if dry_run:
                verdict = (
                    "would refuse: live mentions need superseded_by= or args={'force': True}"
                    if blocked
                    else "would retire"
                )
                replacement = (
                    f"; successor {successor_handle}" if successor_handle else ""
                )
                return Response(
                    body=(
                        f"dry run: {verdict} {self.kind} id={ref_id}"
                        f"{replacement}\n{receipt}\n\n"
                        + dependants_block(
                            self.store,
                            kind=self.kind,
                            ref_id=ref_id,
                            id_arg=str(ref_id),
                            page=deps,
                        )
                    )
                )
            if blocked:
                raise BadInput(
                    f"cannot retire {handle_registry.format_handle(self.kind, ref_id)}: {report.blocking} live mention(s)\n{receipt}",
                    next=[
                        f"delete(kind={self.kind!r}, id={ref_id}, superseded_by='<live handle>')",
                        f"delete(kind={self.kind!r}, id={ref_id}, args={{'force': True}})",
                    ],
                )
            if successor_handle is not None:
                self.store.stamp_ref_meta(
                    ref_id, {"superseded_by": successor_handle}, conn=conn
                )
            counts = conn.execute(
                "SELECT count(*) FILTER (WHERE dst_ref_id = %s"
                "   AND relation <> 'supersedes')::int,"
                "       count(*) FILTER (WHERE src_ref_id = %s)::int,"
                "       count(*) FILTER (WHERE dst_ref_id = %s"
                "   AND relation = 'supersedes')::int"
                "  FROM links WHERE src_ref_id = %s OR dst_ref_id = %s",
                (ref_id, ref_id, ref_id, ref_id, ref_id),
            ).fetchone()
            n_in, n_out, n_sup = (
                (int(counts[0]), int(counts[1]), int(counts[2]))
                if counts
                else (0, 0, 0)
            )
            if successor is not None:
                moved = self.store.migrate_links(
                    ref_id, successor, conn=conn, preserve_supersedes=True
                )
            else:
                # Inbound ``supersedes`` edges stay: "new supersedes old"
                # IS the tombstone trail (dangling_links skips them by
                # design); the Gone reads it back (gr503546).
                cur = conn.execute(
                    "DELETE FROM links WHERE (src_ref_id = %s OR dst_ref_id = %s)"
                    " AND NOT (dst_ref_id = %s AND relation = 'supersedes')",
                    (ref_id, ref_id, ref_id),
                )
                removed = int(cur.rowcount or 0)
            self._record_delete_reason(ref_id, reason, conn=conn)
            self.store.retire_ref(ref_id, conn=conn)
        if successor is not None:
            detail = (
                f"{moved} link(s) migrated to {successor_kind} id={successor} "
                f"(meta.superseded_by)"
            )
        else:
            kept = f"; {n_sup} supersedes edge(s) kept" if n_sup else ""
            detail = (
                f"{n_in} inbound + {n_out} outbound link(s) removed{kept}"
                if (n_in or n_out or removed or n_sup)
                else "no links to remove"
            )
        return Response(
            body=(
                f"deleted {self._sense()} id={ref_id} — {detail}\n{receipt}\n"
                + dependants_summary(self.store, ref_id=ref_id, page=deps)
            )
        )
