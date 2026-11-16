"""MachineHandler — ``kind='machine'``, one ref per host in the fleet.

gr549585 (Reto ruling 2026-11-02): agents and pages needed a first-class
thing to point at for a host — "ran on <host>" links to a machine ref.

Slice 1 shape (the ruling's spec):

* One slug-addressed ref per host, public id ``ma_<host>``. The ref's
  ``meta`` holds **stable facts only** — ``host``, ``platform``,
  ``release``, ``role``, ``gpu`` — mirrored into a ``card_combined``
  chunk (ord=-1) so the ref embeds and searches. Slug addressing and the
  card text live in :mod:`precis.utils.machine_ref` (the heartbeat
  reporter auto-creates refs from there; workers may not import
  handlers).
* ``get`` overlays **live state read at request time** from
  ``host_heartbeat`` (migration 0017, written by
  :mod:`precis.workers.heartbeat`): load average, CPU temperature,
  per-card GPU utilisation/memory and the 1 h/24 h utilisation means,
  and last-seen UTC. Those fields are **never** written into the ref
  body, chunks or embeddings — the graph doesn't churn on a one-minute
  collector. A host with no heartbeat row shows ``no heartbeat``,
  **never** "down" (a daemon-free host is a real state, not a failure).
* ``search`` filters by ``platform`` / ``role`` (case-insensitive
  substring on the meta fact) and ``status`` (``live`` / ``stale`` /
  ``no heartbeat``, derived from heartbeat age against the same 600 s
  horizon the web Status tab uses). Unfiltered lists every ref.
* ``view='daemons'`` (gr627361) — the read-only release daemon cohort:
  every live host that attests for a deploy (the shared
  :mod:`precis.daemon_fleet` read ``release_confirm`` counts), with
  each host's heartbeat ``code_sha`` — the last-beating process's boot
  version — and beat time beside the installed ``worker_units``
  snapshot, so installed metadata is never read as the running version.
  The ``processes`` column carries the per-process evidence
  (``meta.code_sha_by_process``: each named process's own sha and beat
  age); persistent ``activity``/``boot_ids`` merge keys are marked as
  such, never listed as live processes (td649547). A published
  ``deploy.fleet`` ``covered`` list gates membership — an uncovered
  host's worker evidence does not admit it; ephemeral, container-id
  and stale (>60 min) beats are excluded; live non-attestors show as
  a note, never counted (gr623769).
  The same ``code_sha`` / units facts overlay the single-host ``get``.
* ``view='gpu'`` (gr592809 slice c) — the fleet GPU line for the weekly
  report: per GPU host, the card count (live heartbeat ``gpu_stats``,
  else the ``resource_slots`` capacity), the 24 h mean GPU utilisation
  from ``host_heartbeat_log``, and GPU seed slots in use / capacity
  from ``resource_slots``.
* Refs auto-create on heartbeat (gr592809 slice b):
  :func:`precis.utils.machine_ref.ensure_machine_ref` mints ``ma_<host>``
  on the first beat from an unseen, non-retired, non-ephemeral host —
  no manual step. ``put(kind='machine', mode='sync')`` remains the
  hand-refresh: it upserts one ref per host in ``host_heartbeat`` (plus
  any extra ``hosts=[...]`` named in args — a daemon-free host that
  never beats still gets a ref), syncing ``platform``/``release``
  facts while preserving hand-set ``role``/``gpu``. There is no
  deploy-inventory reader in the tree to source more hosts from, so
  heartbeat plus ``hosts=`` is the whole census.
* ``put(kind='machine', id='<host|ma_<host>>', args={platform, role,
  gpu})`` is the manual upsert for one ref — the door for facts the
  collector can't see (GPU model, role).

Linkable like every other slug kind: ``link(kind='job', id=…,
target='machine:ma_<host>')`` resolves through the normal link-target
path — no new relation. Capability sourcing (deploy config,
service_config, worker lanes) is slice 2 — see
`gr692107`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

from precis import daemon_fleet
from precis.dispatch import Hub, InitError
from precis.errors import BadInput, NotFound
from precis.format import render_agent_table
from precis.handlers._eye import is_eye_view
from precis.handlers._links_render import render_links_section, render_links_view
from precis.handlers._slug_ref_shared import search_hits_slug_refs
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.store._heartbeat_ops import HostHeartbeat
from precis.store.types import ChunkInsert, Ref
from precis.utils import handle_registry
from precis.utils.link_tag_ops import (
    apply_link_ops,
    apply_tag_ops,
    format_link_tag_ack,
    require_link_target,
    require_tag_ops,
    validate_link_mode,
)
from precis.utils.machine_ref import FACT_KEYS, card_text, host_for, slug_for
from precis.utils.slug_ref import resolve_live_slug_ref

#: A host is ``live`` while its heartbeat is this fresh — the same 600 s
#: horizon the web Status tab flags quiet hosts with
#: (``precis_web/routes/status.py::_STALE_AFTER_S``) and nursery's
#: ``HOST_DARK_SILENCE_MIN`` dark detector uses. Past it the machine is
#: ``stale``; with no row at all it is ``no heartbeat``.
_LIVE_AFTER_S = 600

_SLUG_PREFIX = "ma_"


def status_of(hb: HostHeartbeat | None, *, now: datetime) -> str:
    """The request-time status derivation — ``live`` / ``stale`` /
    ``no heartbeat`` — from one ``host_heartbeat`` row's age."""
    if hb is None:
        return "no heartbeat"
    age = (now - hb.ts).total_seconds()
    return "live" if age <= _LIVE_AFTER_S else "stale"


def _age_label(hb: HostHeartbeat | None, *, now: datetime) -> str:
    if hb is None:
        return "never"
    return _beat_age(hb.ts, now=now)


def _beat_age(ts: datetime, *, now: datetime) -> str:
    age = max(0.0, (now - ts).total_seconds())
    if age < 90:
        return f"{int(age)}s ago"
    if age < 90 * 60:
        return f"{age / 60:.0f}m ago"
    if age < 48 * 3600:
        return f"{age / 3600:.0f}h ago"
    return f"{age / 86400:.0f}d ago"


def _processes_label(b: daemon_fleet.DaemonBeat, *, now: datetime) -> str:
    """The per-process evidence column (td649547).

    Entries from ``meta.code_sha_by_process`` — each named process's own
    advertised ``{sha, at}`` — render as ``<proc>@<sha12> (<beat age>)``:
    that is per-process boot version AND freshness (a stopped process's
    entry keeps its old ``at``, visibly stale). Keys that only exist in
    the persistent ``activity``/``boot_ids`` merge maps — a stopped
    process's key survives forever, so key presence is never liveness —
    are named but marked as having no per-process evidence.
    """
    parts: list[str] = []
    for proc, ent in sorted(b.process_shas.items()):
        sha = ent.get("sha")
        label = f"{proc}@{str(sha)[:12]}" if sha else f"{proc}@?"
        at = ent.get("at")
        if isinstance(at, datetime):
            label += f" ({_beat_age(at, now=now)})"
        else:
            label += " (no stamp)"
        parts.append(label)
    keys = sorted((set(b.activity) | set(b.boot_ids)) - set(b.process_shas))
    if keys:
        parts.append(", ".join(keys) + " — persistent keys, no per-process evidence")
    return "; ".join(parts) or "—"


def _units_label(worker_units: dict[str, Any] | None) -> str:
    """The installed-metadata column: the ``worker_units`` systemctl
    snapshot — installed unit files and their states, never the running
    version (that is ``code_sha``)."""
    if worker_units is None:
        return "no snapshot"
    units = worker_units.get("units")
    if not isinstance(units, dict):
        return "—"
    if not units:
        return "none installed"
    return ", ".join(
        f"{name}:{(state or {}).get('active_state') or '?'}"
        for name, state in units.items()
    )


def _gpu_card_lines(gpu_stats: Any) -> list[str]:
    """One ``gpu <i>: <name> — util <p>%, mem <used>/<total> MiB`` line per
    card of the heartbeat's live ``gpu_stats`` sample (gr592809).

    A card that answered ``[N/A]`` for a field renders ``n/a`` there —
    unified-memory cards have no MiB counters, and a fabricated 0 would
    read as an idle card. Malformed entries (not a dict) are skipped."""
    if not isinstance(gpu_stats, list):
        return []
    lines = []
    for g in gpu_stats:
        if not isinstance(g, dict):
            continue
        util = g.get("util_pct")
        used = g.get("mem_used_mib")
        total = g.get("mem_total_mib")
        util_s = f"{util:.0f}%" if isinstance(util, (int, float)) else "n/a"
        if isinstance(used, (int, float)) and isinstance(total, (int, float)):
            mem_s = f"{int(used)}/{int(total)} MiB"
        else:
            mem_s = "n/a"
        lines.append(
            f"gpu {g.get('index', '?')}: {g.get('name', '?')} — util {util_s}, mem {mem_s}"
        )
    return lines


def _ts_z(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mh(ref_id: int) -> str:
    return handle_registry.format_handle("machine", ref_id)


class MachineHandler(Handler):
    """Slug-addressed host ref; live state overlaid from host_heartbeat."""

    #: heartbeat-reported facts written by sync, not agent prose: the
    #: agent-write secret gate (dispatch) skips this kind.
    stores_opaque_text: ClassVar[bool] = True

    spec: ClassVar[KindSpec] = KindSpec(
        kind="machine",
        title="Machine",
        description=(
            "A host in the fleet — one ref per machine, id 'ma_<host>'. "
            "The ref body holds stable facts only (platform, role, GPU); "
            "get(kind='machine', id='ma_<host>') overlays live load, CPU "
            "temperature, per-card GPU utilisation/memory and the 1h/24h "
            "utilisation means, all read at request time from "
            "host_heartbeat — 'no heartbeat' (never 'down') when the host "
            "has no row. Refs auto-create on a host's first heartbeat; "
            "put(kind='machine', mode='sync') is the manual census "
            "refresh, or put(kind='machine', id='<host>', "
            "args={'platform':…,'role':…,'gpu':…}) for one. "
            "search(kind='machine') filters by platform=, "
            "role= and status= ('live'|'stale'|'no heartbeat'); "
            "unfiltered lists all. "
            "get(kind='machine', view='gpu') is the fleet GPU line — per "
            "GPU host: cards, 24h mean GPU utilisation, GPU seed slots "
            "in use / capacity. "
            "get(kind='machine', view='daemons') reads the release daemon "
            "cohort — each attestor's heartbeat code_sha (last-beating "
            "process's boot version) plus per-process sha/beat evidence "
            "where advertised, the same cohort release_confirm "
            "attests. Linkable — "
            "link(target='machine:ma_<host>'). See precis-machine-help."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        supports_put=True,
        supports_tag=True,
        supports_link=True,
        is_numeric=False,
        id_required=False,
        modes=("sync",),
        views=("daemons", "gpu", "links"),
    )

    kind: ClassVar[str] = "machine"

    def __init__(self, *, hub: Hub) -> None:
        if hub.store is None:
            raise InitError("machine: store required")
        self.store = hub.store
        self.hub = hub

    # ── get ──────────────────────────────────────────────────────────

    def get(
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
        q: str | None = None,
        rel: str | None = None,
        of_kind: str | None = None,
        limit: Any = None,
        cursor: str | None = None,
        **_kw: Any,
    ) -> Response:
        if id is None:
            if view == "daemons":
                if q is not None:
                    raise BadInput(
                        "view='daemons' takes no q= — it is the whole "
                        "release daemon cohort",
                        next="get(kind='machine', view='daemons')",
                    )
                return self._daemons_view()
            if view == "gpu":
                if q is not None:
                    raise BadInput(
                        "view='gpu' takes no q= — it is the whole fleet's GPU line",
                        next="get(kind='machine', view='gpu')",
                    )
                return self._gpu_view()
            if view is not None and not is_eye_view(view):
                raise BadInput(
                    f"kind='machine' has no view {view!r} without id=",
                    next="get(kind='machine', view='daemons') reads the "
                    "release daemon cohort; view='gpu' the fleet GPU "
                    "line; search(kind='machine') lists all; "
                    "get(kind='machine', id='ma_<host>') reads one",
                )
            return self.search(q=q)
        if isinstance(id, list):
            raise BadInput(
                "get(kind='machine') takes one id",
                next="get(kind='machine', id='ma_<host>')",
            )
        if view is not None:
            if is_eye_view(view):
                return self.eye(id=id, extent=view)
            if view == "daemons":
                raise BadInput(
                    "machine view='daemons' takes no id= — it reads the whole cohort",
                    next="get(kind='machine', view='daemons'); "
                    "get(kind='machine', id='ma_<host>') shows one host's "
                    "code_sha",
                )
            if view == "gpu":
                raise BadInput(
                    "machine view='gpu' takes no id= — it reads the whole "
                    "fleet's GPU line",
                    next="get(kind='machine', view='gpu'); "
                    "get(kind='machine', id='ma_<host>') shows one host's "
                    "per-card utilisation",
                )
            if view != "links":
                raise BadInput(
                    f"kind='machine' has no view {view!r}",
                    next="get(kind='machine', id='ma_<host>', view='links')",
                )
            return render_links_view(
                self.store,
                self._resolve_ref(id),
                sense="machine",
                rel=rel,
                of_kind=of_kind,
                limit=limit,
                cursor=cursor,
            )
        ref = self._resolve_ref(id)
        hb = self._heartbeat_map().get((ref.meta or {}).get("host", ""))
        return Response(body=self._render_one(ref, hb))

    # ── search ───────────────────────────────────────────────────────

    def search(
        self,
        *,
        q: str | None = None,
        platform: str | None = None,
        role: str | None = None,
        status: str | None = None,
        view: str | None = None,
        page_size: int = 50,
        **_kw: Any,
    ) -> Response:
        if view is not None:
            if view != "daemons":
                raise BadInput(
                    f"kind='machine' has no search view {view!r}",
                    next="search(kind='machine') lists all; "
                    "view='daemons' reads the release daemon cohort",
                )
            if any(f is not None for f in (q, platform, role, status)):
                raise BadInput(
                    "view='daemons' takes no filters — it is the whole "
                    "release daemon cohort, already freshness- and "
                    "membership-gated",
                    next="get(kind='machine', view='daemons') — or drop "
                    "view= to filter refs",
                )
            return self._daemons_view()
        want_status = self._parse_status(status)
        if q is not None and q.strip():
            pairs = self.store.search_refs_lexical(
                q=q.strip(), kind=self.kind, limit=max(page_size, 500)
            )
            refs = [r for r, _rank in pairs]
        else:
            refs = self.store.list_refs(kind=self.kind, limit=5000)

        hbs = self._heartbeat_map()
        now = datetime.now(UTC)
        rows: list[dict[str, Any]] = []
        for ref in refs:
            meta = ref.meta or {}
            if platform is not None and not self._fact_match(
                meta.get("platform"), platform
            ):
                continue
            if role is not None and not self._fact_match(meta.get("role"), role):
                continue
            hb = hbs.get(str(meta.get("host") or ""))
            st = status_of(hb, now=now)
            if want_status is not None and st != want_status:
                continue
            rows.append(
                {
                    "handle": _mh(ref.id),
                    "id": ref.slug or "?",
                    "status": st,
                    "last_seen": (
                        f"{_ts_z(hb.ts)} ({_age_label(hb, now=now)})"
                        if hb is not None
                        else "—"
                    ),
                    "load1": f"{hb.load1:.2f}" if hb and hb.load1 is not None else "—",
                    "temp_c": f"{hb.temp_c:.0f}"
                    if hb and hb.temp_c is not None
                    else "—",
                    "platform": str(meta.get("platform") or "—"),
                    "role": str(meta.get("role") or "—"),
                }
            )

        head_bits = []
        if q is not None and q.strip():
            head_bits.append(f"q={q.strip()!r}")
        if platform is not None:
            head_bits.append(f"platform={platform!r}")
        if role is not None:
            head_bits.append(f"role={role!r}")
        if want_status is not None:
            head_bits.append(f"status={want_status!r}")
        head = " ".join(head_bits) or "all machines"
        lines = [f"# {len(rows)} machine(s) — {head}"]
        if not rows:
            lines.append(
                "none — put(kind='machine', mode='sync') refreshes the "
                "refs from host_heartbeat; "
                "put(kind='machine', id='<host>', args={...}) adds one"
            )
            return Response(body="\n".join(lines))
        rows = rows[:page_size]
        lines.append(
            render_agent_table(
                rows,
                schema=[
                    "handle",
                    "id",
                    "status",
                    "last_seen",
                    "load1",
                    "temp_c",
                    "platform",
                    "role",
                ],
            )
        )
        return Response(body="\n".join(lines))

    def search_hits(
        self,
        *,
        q: str | None = None,
        page_size: int = 10,
        **_kw: Any,
    ):
        """Cross-kind merge entry point — title/slug lexical leg."""
        return search_hits_slug_refs(
            self.store, kind=self.kind, q=q, page_size=page_size
        )

    def accepted_views(self, *, id: Any = None) -> list[str]:
        return ["daemons", "gpu", "links"]

    # ── view='gpu' — the fleet GPU line (gr592809 slice c) ─────────────

    def _gpu_view(self) -> Response:
        """The weekly report's fleet GPU line: per GPU host — the card
        count, the 24 h mean GPU utilisation from ``host_heartbeat_log``,
        and GPU seed slots in use / capacity from ``resource_slots``.

        A ref counts as a GPU host when its latest heartbeat carries
        ``gpu_stats`` or a ``resource_slots`` ``gpu`` row exists for the
        host (a Mac with neither is not listed, not shown as zero-GPU).
        Card count reads the live heartbeat first — the probe one
        ``--query-gpu`` away — falling back to the slot capacity the
        capability probe seeded."""
        hbs = self._heartbeat_map()
        gpu_slots = {
            s.host: s for s in self.store.all_resource_slots() if s.resource == "gpu"
        }
        rows: list[dict[str, Any]] = []
        for ref in self.store.list_refs(kind=self.kind, limit=5000):
            meta = ref.meta or {}
            host = str(meta.get("host") or host_for(ref.slug or ""))
            hb = hbs.get(host)
            stats = hb.meta.get("gpu_stats") if hb is not None else None
            slot = gpu_slots.get(host)
            if not stats and slot is None:
                continue
            if stats:
                gpu_count: str = str(len(stats))
            else:
                gpu_count = str(slot.capacity) if slot is not None else "—"
            means = self.store.heartbeat_means(host, hours=24)
            util = means["gpu_util_pct"]
            rows.append(
                {
                    "machine": slug_for(host),
                    "gpus": gpu_count,
                    "gpu_util_24h": f"{util:.0f}%" if util is not None else "—",
                    "seed_slots": (
                        f"{slot.capacity - slot.free}/{slot.capacity}" if slot else "—"
                    ),
                }
            )
        lines = [f"# fleet GPU line — {len(rows)} GPU host(s)"]
        if not rows:
            lines.append(
                "none — no machine ref has a heartbeat gpu_stats sample or "
                "a resource_slots gpu row yet; the utilisation columns fill "
                "as hosts with nvidia-smi beat (gr592809)"
            )
            return Response(body="\n".join(lines))
        lines.append(
            render_agent_table(
                rows,
                schema=["machine", "gpus", "gpu_util_24h", "seed_slots"],
            )
        )
        lines.append(
            "gpus: live heartbeat gpu_stats card count (else the "
            "resource_slots capacity) · gpu_util_24h: mean of the per-beat "
            "card-mean utilisation over 24 h · seed_slots: GPU slots in use "
            "/ capacity from resource_slots"
        )
        return Response(body="\n".join(lines))

    # ── view='daemons' — the release daemon cohort (gr627361) ────────

    def _daemons_view(self) -> Response:
        """Read-only release-attestation evidence: the same live-fleet
        read :mod:`precis.workers.job_types.release_confirm` counts —
        every configured daemon host's heartbeat ``code_sha`` (the
        host row's last-beating process's boot version) and beat time
        beside the installed ``worker_units`` snapshot, so installed
        metadata is never read as the running version. The processes
        column shows per-process sha/freshness
        (``meta.code_sha_by_process``); surviving ``activity``/
        ``boot_ids`` keys are marked persistent, never read as live
        processes (td649547). Ephemeral, container-id and >60-min-stale
        beats are out; live non-attestors (worker-less, or uncovered by
        a published deploy.fleet) show as a note, never counted
        (gr623769)."""
        beats, noncohort = daemon_fleet.live_fleet(self.store)
        ledger = daemon_fleet.deploy_ledger(self.store)
        now = datetime.now(UTC)
        deployed = ledger.get("sha")
        deploying = ledger.get("deploying")
        head = f"# release daemon cohort — {len(beats)} live host(s)"
        if isinstance(deployed, str) and deployed:
            head += f" — deployed {deployed[:12]}"
        elif not ledger:
            head += " — no deploy.fleet ledger"
        if (
            isinstance(deploying, dict)
            and isinstance(deploying.get("sha"), str)
            and deploying["sha"]
        ):
            head += f" (deploying {deploying['sha'][:12]})"
        lines = [head]
        if beats:
            rows = [
                {
                    "machine": slug_for(b.host),
                    "host": b.host,
                    "code_sha": b.code_sha[:12] if b.code_sha else "—",
                    "last_beat": (
                        f"{_ts_z(b.ts)} ({_beat_age(b.ts, now=now)})"
                        if b.ts is not None
                        else "—"
                    ),
                    "installed_units": _units_label(b.worker_units),
                    "processes": _processes_label(b, now=now),
                }
                for b in beats
            ]
            lines.append(
                render_agent_table(
                    rows,
                    schema=[
                        "machine",
                        "host",
                        "code_sha",
                        "last_beat",
                        "installed_units",
                        "processes",
                    ],
                )
            )
            lines.append(
                "code_sha / last_beat are the host row's last-beating "
                "process — per-process boot versions and beat ages are in "
                "the processes column (meta.code_sha_by_process); names "
                "marked 'persistent keys' have no per-process evidence"
            )
        else:
            lines.append(
                "none — no live daemon heartbeat in the last "
                f"{daemon_fleet.FLEET_FRESH_MIN} min from a non-ephemeral, "
                "non-container host running workers or on the "
                "deploy.fleet ledger"
            )
        if noncohort:
            lines.append(
                "note: "
                + ", ".join(f"{h} ({why})" for h, why in noncohort)
                + " — live but not counted (gr623769, td649547)"
            )
        return Response(body="\n".join(lines))

    # ── put ──────────────────────────────────────────────────────────

    def put(
        self,
        *,
        id: str | int | None = None,
        mode: str | None = None,
        hosts: list[str] | str | None = None,
        platform: str | None = None,
        role: str | None = None,
        gpu: str | None = None,
        tags: list[str] | None = None,
        link: str | None = None,
        rel: str | None = None,
        **_kw: Any,
    ) -> Response:
        if mode == "sync":
            if id is not None:
                raise BadInput(
                    "put(kind='machine', mode='sync') takes no id= — it "
                    "upserts every host in host_heartbeat",
                    next="put(kind='machine', mode='sync') or "
                    "put(kind='machine', id='<host>', args={...}) for one",
                )
            return self._sync(hosts=hosts)
        if mode is not None:
            raise BadInput(
                f"put(kind='machine') has no mode {mode!r}",
                next="mode='sync' refreshes refs from host_heartbeat; "
                "no mode with id= upserts one ref",
            )
        if id is None:
            raise BadInput(
                "put(kind='machine') requires id= (a host name) or mode='sync'",
                next="put(kind='machine', mode='sync') — or "
                "put(kind='machine', id='<host>', args={'platform':…,"
                "'role':…,'gpu':…})",
            )
        facts = {"platform": platform, "role": role, "gpu": gpu}
        ref, created = self._upsert_one(host_for(str(id)), facts=facts)
        n_tags = n_links = 0
        if tags:
            n_tags, _ = apply_tag_ops(
                self.store, self.kind, ref.id, tags=tags, untags=None
            )
        if link:
            n_links, _ = apply_link_ops(
                self.store,
                ref.id,
                link=link,
                unlink=None,
                rel=rel,
                hub=getattr(self, "hub", None),
            )
        tail = ""
        if n_tags or n_links:
            tail = f" (+{n_tags} tag(s), +{n_links} link(s))"
        return Response(
            body=(
                f"{'created' if created else 'updated'} machine {ref.slug} "
                f"({_mh(ref.id)}){tail} — live state overlays at get time; "
                "mode='sync' refreshes the whole census"
            )
        )

    # ── tag / link ───────────────────────────────────────────────────

    def tag(
        self,
        *,
        id: str | int | None = None,
        add: list[str] | None = None,
        remove: list[str] | None = None,
        **_kw: Any,
    ) -> Response:
        require_tag_ops(self.kind, add, remove)
        ref = self._resolve_ref(id)
        n_add, n_rem = apply_tag_ops(
            self.store, self.kind, ref.id, tags=add, untags=remove
        )
        return Response(
            body=format_link_tag_ack(
                kind=self.kind,
                ref_label=_mh(ref.id),
                n_links_added=0,
                n_links_removed=0,
                n_tags_added=n_add,
                n_tags_removed=n_rem,
            )
        )

    def link(
        self,
        *,
        id: str | int | None = None,
        target: str | None = None,
        mode: str = "add",
        rel: str | None = None,
        **_kw: Any,
    ) -> Response:
        target = require_link_target(self.kind, target)
        validate_link_mode(mode)
        ref = self._resolve_ref(id)
        n_add, n_rem = apply_link_ops(
            self.store,
            ref.id,
            link=target if mode == "add" else None,
            unlink=target if mode == "remove" else None,
            rel=rel,
            hub=getattr(self, "hub", None),
        )
        return Response(
            body=format_link_tag_ack(
                kind=self.kind,
                ref_label=_mh(ref.id),
                n_links_added=n_add,
                n_links_removed=n_rem,
                n_tags_added=0,
                n_tags_removed=0,
            )
        )

    # ── sync / upsert internals ──────────────────────────────────────

    def _sync(self, *, hosts: list[str] | str | None) -> Response:
        """Upsert one ref per host in ``host_heartbeat`` (+ ``hosts=``)."""
        created = updated = 0
        seen: list[str] = []
        for hb in self.store.recent_heartbeats():
            # A host marked retired (``precis heartbeat --retire`` stamps
            # meta.retired) keeps any existing ref but gains no new one —
            # the fleet census sync should not resurrect a decommissioned
            # machine. Its last-seen state still reads 'stale'.
            if hb.meta.get("retired"):
                continue
            _, was_created = self._upsert_one(
                hb.host,
                facts={
                    "platform": hb.meta.get("platform"),
                    "release": hb.meta.get("release"),
                },
            )
            seen.append(hb.host)
            created += was_created
            updated += not was_created
        extra = [hosts] if isinstance(hosts, str) else list(hosts or [])
        for host in extra:
            host = str(host).strip()
            if not host or host in seen:
                continue
            _, was_created = self._upsert_one(host, facts={})
            created += was_created
            updated += not was_created
        return Response(
            body=(
                f"machine sync: {created} created, {updated} updated "
                f"({len(seen)} host_heartbeat row(s)"
                + (f", +{len(extra)} extra host(s)" if extra else "")
                + ") — live state overlays at get time, never stored"
            )
        )

    def _upsert_one(
        self, host: str, *, facts: dict[str, Any] | None
    ) -> tuple[Ref, bool]:
        """Insert or refresh the ``ma_<host>`` ref + its card chunk.

        ``meta || patch`` merge semantics: keys absent from ``facts``
        keep their stored values, so a sync never clobbers a
        hand-entered ``role``/``gpu``. Returns ``(ref, created)``.
        """
        host = host.strip()
        slug = slug_for(host)
        if not slug:
            raise BadInput(
                f"machine host {host!r} yields no usable slug",
                next="put(kind='machine', id='<host>', args={...}) — "
                "letters, digits, '.', '_', '-' only",
            )
        meta_patch: dict[str, Any] = {"host": host}
        for k, v in (facts or {}).items():
            if k not in FACT_KEYS:
                raise BadInput(
                    f"machine meta has no fact {k!r}",
                    next=f"facts are {list(FACT_KEYS)} — stable facts "
                    "only; load/temperature live in host_heartbeat",
                )
            # ``meta || patch`` can't delete a key — skip Nones so a
            # factless sync never erases a hand-set value.
            if v not in (None, ""):
                meta_patch[k] = v
        existing = self.store.get_ref(kind=self.kind, id=slug)
        base_meta = (existing.meta or {}) if existing is not None else {}
        card = card_text(host, {**base_meta, **meta_patch})
        # embedding stays NULL — the embed worker's fill_embeddings pass
        # claims it; ingest paths never embed inline (invariants).
        card_block = ChunkInsert(
            ord=-1,
            text=card,
            meta={"chunk_kind": "card_combined"},
        )
        with self.store.tx() as conn:
            if existing is None:
                ref = self.store.insert_ref(
                    kind=self.kind,
                    slug=slug,
                    title=host,
                    meta=meta_patch,
                    conn=conn,
                )
            else:
                ref = self.store.update_ref(
                    existing.id, title=host, meta_patch=meta_patch, conn=conn
                )
            self.store.chunks.insert_chunks(
                ref.id, [card_block], replace=True, conn=conn
            )
        return ref, existing is None

    # ── resolution / render internals ────────────────────────────────

    def _resolve_ref(self, id: str | int | None) -> Ref:
        if id is None:
            raise BadInput(
                "machine id required",
                next="search(kind='machine') lists refs; "
                "get(kind='machine', id='ma_<host>') reads one",
            )
        if isinstance(id, int):
            ref = self.store.get_ref(kind=self.kind, id=id)
            if ref is None:
                raise NotFound(
                    f"no machine ref_id {id}",
                    next="search(kind='machine') lists refs",
                )
            return ref
        raw = str(id).strip()
        if "~" in raw:
            raise BadInput(
                "machine ops operate at ref level — drop the '~' selector from id=",
                next="get(kind='machine', id='ma_<host>')",
            )
        if raw.startswith("machine:"):
            raw = raw[len("machine:") :]
        parsed = handle_registry.parse(raw)
        if parsed is not None and parsed[0] == self.kind and not parsed[1]:
            return self._resolve_ref(parsed[2])
        if raw.isdigit():
            return self._resolve_ref(int(raw))
        slug = raw if raw.startswith(_SLUG_PREFIX) else slug_for(host_for(raw))
        return resolve_live_slug_ref(
            self.store,
            kind=self.kind,
            id=slug,
            next_hint="search(kind='machine') lists refs; "
            "put(kind='machine', mode='sync') creates them",
        )

    def _heartbeat_map(self) -> dict[str, HostHeartbeat]:
        return {hb.host: hb for hb in self.store.recent_heartbeats()}

    def _util_means_line(self, host: str) -> str | None:
        """The ``util 1h/24h mean: cpu a%, gpu b%`` overlay line, or None
        when the window has no utilisation sample at all (nothing to
        show beats a line of n/as)."""
        means_1h = self.store.heartbeat_means(host, hours=1)
        means_24h = self.store.heartbeat_means(host, hours=24)
        values = (
            means_1h["cpu_used_pct"],
            means_24h["cpu_used_pct"],
            means_1h["gpu_util_pct"],
            means_24h["gpu_util_pct"],
        )
        if all(v is None for v in values):
            return None

        def _pct(v: float | None) -> str:
            return f"{v:.0f}%" if v is not None else "n/a"

        cpu = f"{_pct(means_1h['cpu_used_pct'])}/{_pct(means_24h['cpu_used_pct'])}"
        gpu = f"{_pct(means_1h['gpu_util_pct'])}/{_pct(means_24h['gpu_util_pct'])}"
        return f"util 1h/24h mean: cpu {cpu}, gpu {gpu}"

    def _render_one(self, ref: Ref, hb: HostHeartbeat | None) -> str:
        meta = ref.meta or {}
        host = str(meta.get("host") or host_for(ref.slug or ""))
        now = datetime.now(UTC)
        lines = [f"# {ref.slug} — {host}  ({_mh(ref.id)})"]
        for k in FACT_KEYS:
            v = meta.get(k)
            if v not in (None, ""):
                lines.append(f"{k}: {v}")
        lines.append("")
        if hb is None:
            lines.append(
                "status: no heartbeat — this host has no host_heartbeat "
                "row; its live state is unknown"
            )
        else:
            lines.append(
                f"status: {status_of(hb, now=now)} — last heartbeat "
                f"{_ts_z(hb.ts)} ({_age_label(hb, now=now)})"
            )
            if hb.load1 is not None or hb.load5 is not None or hb.load15 is not None:
                loads = " / ".join(
                    "—" if v is None else f"{v:.2f}"
                    for v in (hb.load1, hb.load5, hb.load15)
                )
                lines.append(f"load (1/5/15m): {loads}")
            lines.append(
                f"temp_c: {f'{hb.temp_c:.1f}' if hb.temp_c is not None else 'n/a (no sensor)'}"
            )
            # gr592809: per-card GPU utilisation/memory (live sample) and
            # the 1 h/24 h means of the per-beat utilisation series.
            for g in _gpu_card_lines(hb.meta.get("gpu_stats")):
                lines.append(g)
            means_line = self._util_means_line(host)
            if means_line is not None:
                lines.append(means_line)
            # Boot-version evidence (gr627361): code_sha is the serving
            # process's actual boot version; worker_units is installed
            # metadata — never read one as the other.
            code_sha = hb.meta.get("code_sha")
            if code_sha not in (None, ""):
                lines.append(f"code_sha: {code_sha} — the serving boot version")
            wu = hb.meta.get("worker_units")
            units = wu.get("units") if isinstance(wu, dict) else None
            if isinstance(units, dict) and units:
                lines.append(f"worker units (installed): {_units_label(wu)}")
        section = render_links_section(self.store, ref, limit=12)
        if section:
            lines.append(section)
        return "\n".join(lines)

    @staticmethod
    def _fact_match(stored: Any, want: str) -> bool:
        """Case-insensitive substring match on a meta fact value."""
        return want.strip().lower() in str(stored or "").lower()

    @staticmethod
    def _parse_status(status: str | None) -> str | None:
        if status is None:
            return None
        norm = status.strip().lower().replace("-", " ").replace("_", " ")
        if norm in ("*", "all", "any"):
            return None
        if norm not in ("live", "stale", "no heartbeat"):
            raise BadInput(
                f"machine status must be live, stale or 'no heartbeat', got {status!r}",
                next="search(kind='machine', status='live')",
            )
        return norm


__all__ = ["MachineHandler", "host_for", "slug_for", "status_of"]
