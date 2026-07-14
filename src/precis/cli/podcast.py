"""``precis podcast`` — publish/list episodes on the private audio feed.

The reusable publish surface: any producer (a TTS brief pass, a script, you by
hand) drops an audio file into the feed and it lands on the phone via the
``precis web`` ``/podcast/feed.xml`` route. See :mod:`precis.audio_feed`.

    precis podcast add brief.m4a --title "Morning brief" --source brief
    precis podcast list
"""

from __future__ import annotations

import argparse
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from precis import audio_feed


def add_parser(subparsers: Any) -> None:
    p = subparsers.add_parser(
        "podcast", help="Publish/list private audio-feed episodes."
    )
    psub = p.add_subparsers(dest="podcast_cmd", required=True)

    add = psub.add_parser("add", help="Publish an audio file as a new episode.")
    add.add_argument("audio", help="Path to the audio file (mp3/m4a/…).")
    add.add_argument("--title", required=True, help="Episode title.")
    add.add_argument("--desc", default="", help="Episode description.")
    add.add_argument("--source", default=None, help="Producer tag (e.g. brief, news).")
    add.add_argument("--duration", type=int, default=None, help="Duration seconds.")
    add.add_argument(
        "--dir", default=None, help="Podcast dir (else PRECIS_PODCAST_DIR)."
    )

    ls = psub.add_parser("list", help="List published episodes (newest first).")
    ls.add_argument(
        "--dir", default=None, help="Podcast dir (else PRECIS_PODCAST_DIR)."
    )


def _resolve_dir(args: argparse.Namespace) -> Path:
    d = args.dir or os.environ.get("PRECIS_PODCAST_DIR")
    if not d:
        raise SystemExit("no podcast dir — pass --dir or set PRECIS_PODCAST_DIR")
    return Path(d).expanduser()


def _episode_id(title: str, when: datetime) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "episode"
    return f"{when.strftime('%Y%m%d%H%M%S')}-{slug}"


def run(args: argparse.Namespace) -> None:
    podcast_dir = _resolve_dir(args)
    if args.podcast_cmd == "add":
        now = datetime.now(tz=UTC)
        ep = audio_feed.publish_episode(
            podcast_dir,
            args.audio,
            episode_id=_episode_id(args.title, now),
            title=args.title,
            description=args.desc,
            published_at=now,
            duration_seconds=args.duration,
            source=args.source,
        )
        print(f"published episode {ep.id} ({ep.bytes} bytes, {ep.mime})")
        return
    if args.podcast_cmd == "list":
        eps = audio_feed.list_episodes(podcast_dir)
        if not eps:
            print("(no episodes)")
            return
        for ep in eps:
            when = ep.published_at.strftime("%Y-%m-%d %H:%M")
            print(f"{when}  {ep.id}  {ep.title!r}  [{ep.source or '-'}]")
        return
