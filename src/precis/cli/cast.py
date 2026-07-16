"""``precis cast`` — compose + schedule the daily audio *casts*.

Two standing casts ride the produce → narrate → publish spine (see
docs/design/reading-prep-loop.md §Audio):

- ``reading`` — the morning situational-awareness brief (voice ``bm_george``).
- ``nidra``   — the evening conceptual-walk meditation (voice ``af_nicole``).

    precis cast run reading            # compose today's morning-brief draft
    precis cast run nidra --publish    # compose + narrate + publish (on spark)
    precis cast schedule --now         # install the daily watches + cast both now

``run`` is the writer organ (node-agnostic — store + LLM → a draft); ``--publish``
additionally narrates it inline via the shipped audio path (needs Kokoro / the
precis-tts image, i.e. spark). ``schedule`` idempotently installs the two
``level:recurring`` watches that drive the ``reading_brief`` / ``meditation``
coordinator job_types on a daily cron; ``--now`` also composes both immediately so
the first episodes don't wait for the next cron boundary (a never-fired daily cron
only fires at its next matching minute).
"""

from __future__ import annotations

import argparse
import os
from typing import Any

from precis.cli._common import resolve_dsn
from precis.reading.cast_common import CAST_PROFILES
from precis.store import Store

_CASTS = ("reading", "nidra")


def add_parser(subparsers: Any) -> None:
    p = subparsers.add_parser("cast", help="Compose/schedule the daily audio casts.")
    csub = p.add_subparsers(dest="cast_cmd", required=True)

    run_p = csub.add_parser("run", help="Compose one cast now (optionally publish).")
    run_p.add_argument("cast", choices=_CASTS, help="Which cast to compose.")
    run_p.add_argument(
        "--date", default=None, help="Override the date tag (YYYY-MM-DD)."
    )
    run_p.add_argument(
        "--publish",
        action="store_true",
        help="Also narrate + publish to the feed (needs Kokoro / the TTS image).",
    )
    run_p.add_argument("--speed", type=float, default=1.0, help="Narration speed.")
    run_p.add_argument("--database-url", default=None, help="Postgres DSN override.")

    sch_p = csub.add_parser(
        "schedule", help="Install the daily cast watches (idempotent)."
    )
    sch_p.add_argument(
        "--now",
        action="store_true",
        help="Also compose both casts immediately (don't wait for the cron).",
    )
    sch_p.add_argument("--database-url", default=None, help="Postgres DSN override.")


def _compose(store: Store, cast: str, *, date_tag: str | None = None) -> int | None:
    """Run the writer organ for ``cast`` → a draft ref id (or None)."""
    if cast == "reading":
        from precis.reading.briefing_cast import build_reading_briefing

        return build_reading_briefing(store, date_tag=date_tag)
    from precis.reading.meditation import build_meditation

    return build_meditation(store, date_tag=date_tag)


def _publish(store: Store, draft_id: int, *, speed: float) -> None:
    """Narrate + publish a just-composed cast draft inline (spark path)."""
    from precis.workers.cast_audio import narrate_cast_ref

    ref = store.get_ref(kind="draft", id=draft_id)
    if ref is None:  # pragma: no cover - just composed, should exist
        print(f"cast: draft {draft_id} vanished before publish")
        return
    image = os.environ.get("PRECIS_TTS_IMAGE")
    synth = None
    if not image:
        from precis.tts.kokoro import KokoroSynth

        synth = KokoroSynth(speed=speed)
    r = narrate_cast_ref(
        store,
        ref,
        image=image,
        synth=synth,
        podcast_dir=os.environ.get("PRECIS_PODCAST_DIR"),
        speed=speed,
        container_cmd=os.environ.get("PRECIS_TTS_CONTAINER_CMD") or "podman",
        scratch_dir=os.environ.get("PRECIS_TTS_SCRATCH"),
    )
    if r["published"]:
        print(
            f"cast: published {r['episode_id']} "
            f"({r['segments']} seg, {r['duration_s']:.0f}s)"
        )
    else:
        print(f"cast: not published ({r['reason']})")


def _find_cast_watch(store: Store, cast: str) -> int | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM refs WHERE kind='todo' AND deleted_at IS NULL "
            "AND meta->>'cast_watch' = %s LIMIT 1",
            (cast,),
        ).fetchone()
    return int(row[0]) if row else None


def _cmd_run(store: Store, args: argparse.Namespace) -> None:
    draft_id = _compose(store, args.cast, date_tag=args.date)
    if draft_id is None:
        print(f"cast {args.cast}: nothing to compose")
        return
    print(f"cast {args.cast}: composed draft ref {draft_id}")
    if args.publish:
        _publish(store, draft_id, speed=args.speed)


#: The morning card pass fires before the 06:00 reading brief so today's new /
#: reworked cards exist when the brief's recall lane composes.
_CARD_FORGE_CRON = "30 5 * * *"


def install_cast_watches(store: Store) -> list[int]:
    """Idempotently install the daily reading-loop watches under the Watches
    umbrella — the two casts plus the 05:30 ``card_forge`` morning card pass.
    Returns the ref ids (existing or freshly created).

    Authors the recurring todos directly (no booted hub needed from a CLI),
    mirroring ``ensure_watches_root``: an ``insert_ref`` + the ``level:recurring``
    gradient tag + ``STATUS:open``, carrying the ``meta.schedule`` /
    ``meta.executor`` / ``meta.job_type`` / ``meta.params`` the recurring spawner
    reads. Idempotent on a ``meta.cast_watch`` marker.
    """
    from precis.store.types import Tag
    from precis.workers.schedule import validate_schedule
    from precis.workers.schedule.seed import ensure_watches_root

    watches = ensure_watches_root(store)
    out: list[int] = []
    for cast in _CASTS:
        profile = CAST_PROFILES[cast]
        existing = _find_cast_watch(store, cast)
        if existing is not None:
            out.append(existing)
            print(f"cast {cast}: watch already installed (ref {existing})")
            continue
        sched = validate_schedule({"cron": profile.cron})
        ref = store.insert_ref(
            kind="todo",
            slug=None,
            title=f"Cast watch: {cast} ({profile.title})",
            meta={
                "schedule": {
                    "cron": sched.cron,
                    "backfill_missed": sched.backfill_missed,
                },
                # claude_inproc (melchior) — the compose uses claude-opus via the
                # melchior-loopback litellm proxy; TTS is a separate downstream
                # pass on spark, so this once-a-day melchior compute is fine.
                "executor": "claude_inproc",
                "job_type": profile.job_type,
                "params": {},
                "cast_watch": cast,
            },
            prio=2,  # the cron tier; note: prio doesn't affect job-execution order
            parent_id=watches,
        )
        store.add_tag(ref.id, Tag.open("level:recurring"), set_by="system")
        store.add_tag(ref.id, Tag.closed("STATUS", "open"), set_by="system")
        out.append(int(ref.id))
        print(
            f"cast {cast}: scheduled @ '{profile.cron}' → job_type {profile.job_type}"
        )

    # The morning card pass — not a cast (no draft, no narration), but part of
    # the same daily loop, so it installs alongside the cast watches.
    existing = _find_cast_watch(store, "card_forge")
    if existing is not None:
        out.append(existing)
        print(f"card_forge: watch already installed (ref {existing})")
        return out
    sched = validate_schedule({"cron": _CARD_FORGE_CRON})
    ref = store.insert_ref(
        kind="todo",
        slug=None,
        title="Cast watch: card_forge (\U0001f0cf morning card work)",
        meta={
            "schedule": {"cron": sched.cron, "backfill_missed": sched.backfill_missed},
            "executor": "claude_inproc",
            "job_type": "card_forge",
            "params": {},
            "cast_watch": "card_forge",
        },
        prio=2,
        parent_id=watches,
    )
    store.add_tag(ref.id, Tag.open("level:recurring"), set_by="system")
    store.add_tag(ref.id, Tag.closed("STATUS", "open"), set_by="system")
    out.append(int(ref.id))
    print(f"card_forge: scheduled @ '{_CARD_FORGE_CRON}' → job_type card_forge")
    return out


def _cmd_schedule(store: Store, args: argparse.Namespace) -> None:
    install_cast_watches(store)

    if args.now:
        print("cast: composing both casts now (first-fire, don't wait for cron)…")
        for cast in _CASTS:
            draft_id = _compose(store, cast)
            if draft_id is None:
                print(f"cast {cast}: nothing to compose yet")
            else:
                print(f"cast {cast}: composed draft ref {draft_id}")


def run(args: argparse.Namespace) -> None:
    store = Store.connect(resolve_dsn(args.database_url))
    if args.cast_cmd == "run":
        _cmd_run(store, args)
    elif args.cast_cmd == "schedule":
        _cmd_schedule(store, args)
