"""``precis draft …`` — operations on the ``draft`` kind (ADR 0033).

Subcommands:

* ``precis draft export <slug> [--out DIR] [--include-sources]`` — render a
  draft into a compilable LaTeX project (``main.tex`` + ``refs.bib`` + a copy
  of the standard ``preamble.tex``). The output is **disposable** — re-export
  from the draft, never hand-edit. Compile with ``latexmk -pdf main.tex``
  (biber + makeglossaries run automatically). ``--include-sources`` bundles
  every cited paper/datasheet PDF the host holds and appends them as a
  ``pdfpages`` appendix (see ``precis.export.sources``).
* ``precis draft papers <slug> [--out FILE]`` — zip the draft's cited
  paper/datasheet PDFs + a manifest.txt bibliography.

The compile + LLM-repair loop and the Word/pandoc path land as later
subcommands of ``precis draft``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from precis.cli._common import resolve_dsn
from precis.export.latex import export_draft
from precis.export.sources import build_sources_zip
from precis.store import Store


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("draft", help="Operate on draft documents (export, …).")
    dsub = p.add_subparsers(dest="draft_cmd", required=True)

    ex = dsub.add_parser(
        "export",
        help="Render a draft to a compilable LaTeX project.",
        description=(
            "Resolve a draft's chunks into main.tex + refs.bib + a copy "
            "of the standard preamble. Cross-refs (¶), citations (§/paper:), "
            "and defined abbreviations are all resolved automatically. "
            "Output is disposable — re-export, don't hand-edit."
        ),
    )
    ex.add_argument("slug", help="Draft slug or numeric ref id.")
    ex.add_argument(
        "--out",
        default=None,
        help="Output directory for the LaTeX project. Default: ./export/<slug>/.",
    )
    ex.add_argument(
        "--format",
        choices=("tex",),
        default="tex",
        help="Export format. Only 'tex' so far (docx via pandoc is a later increment).",
    )
    ex.add_argument(
        "--pdf",
        action="store_true",
        help="Also compile the project to PDF with latexmk (biber + "
        "makeglossaries run automatically). No-op with a warning if "
        "latexmk isn't installed.",
    )
    ex.add_argument(
        "--include-sources",
        action="store_true",
        help="Bundle every cited paper/datasheet PDF the host holds into "
        "sources/ and append them as a pdfpages appendix, so the compiled "
        "PDF is self-contained. Sources not on this host are listed as "
        "warnings.",
    )
    ex.add_argument(
        "--database-url",
        default=None,
        help="Override PRECIS_DATABASE_URL.",
    )

    pz = dsub.add_parser(
        "papers",
        help="Zip up a draft's cited paper/datasheet PDFs + a manifest.",
        description=(
            "Resolve a draft's cited sources (papers, patents, datasheets) to "
            "the PDFs this host holds and write them to a .zip alongside a "
            "manifest.txt bibliography. Sources the host can't locate are "
            "listed in the manifest."
        ),
    )
    pz.add_argument("slug", help="Draft slug or numeric ref id.")
    pz.add_argument(
        "--out",
        default=None,
        help="Output .zip path. Default: ./export/<slug>-papers.zip.",
    )
    pz.add_argument(
        "--database-url",
        default=None,
        help="Override PRECIS_DATABASE_URL.",
    )

    au = dsub.add_parser(
        "audio",
        help="Narrate a draft to audio (Kokoro TTS) — the voice-draft export.",
        description=(
            "Render a draft to an mp3 via the narration layer + Kokoro. Per-"
            "chunk meta.voice/lang route each segment to its narrator. Needs "
            "the [tts] extra + PRECIS_KOKORO_MODEL/VOICES + ffmpeg on this "
            "host. --publish drops it on the private podcast feed."
        ),
    )
    au.add_argument("slug", help="Draft slug or numeric ref id.")
    au.add_argument(
        "--out", default=None, help="Output .mp3. Default: ./export/<slug>.mp3."
    )
    au.add_argument(
        "--voice", default="af_heart", help="Default voice (chunk meta overrides)."
    )
    au.add_argument("--lang", default="en-us", help="Default language.")
    au.add_argument("--speed", type=float, default=1.0, help="Speaking rate (0.5–2.0).")
    au.add_argument(
        "--max-segments", type=int, default=None, help="Cap segments (preview)."
    )
    au.add_argument(
        "--publish", action="store_true", help="Publish to the podcast feed."
    )
    au.add_argument(
        "--podcast-dir", default=None, help="Feed dir (else PRECIS_PODCAST_DIR)."
    )
    au.add_argument(
        "--database-url", default=None, help="Override PRECIS_DATABASE_URL."
    )


def run(args: argparse.Namespace) -> None:
    if args.draft_cmd == "export":
        _run_export(args)
        return
    if args.draft_cmd == "papers":
        _run_papers(args)
        return
    if args.draft_cmd == "audio":
        _run_audio(args)
        return
    print(f"draft: unknown subcommand {args.draft_cmd!r}", file=sys.stderr)
    sys.exit(2)


def _run_audio(args: argparse.Namespace) -> None:
    import os
    import tempfile
    from datetime import UTC, datetime

    from precis import audio_feed
    from precis.draft.narrate import load_personal_lexicon, resolve_lexicon
    from precis.export.audio import export_audio
    from precis.tts.kokoro import KokoroSynth

    dsn = resolve_dsn(args.database_url)
    store = Store.connect(dsn)
    try:
        key: int | str = int(args.slug) if str(args.slug).isdigit() else args.slug
        ref = store.get_ref(kind="draft", id=key)
        if ref is None:
            print(f"draft audio: no draft {args.slug!r}", file=sys.stderr)
            sys.exit(2)
        # Two-level pronunciation lexicon: personal base (PRECIS_LEXICON_FILE)
        # under the draft's own meta.pronunciation overrides.
        lexicon = resolve_lexicon(ref, personal=load_personal_lexicon())
        out = (
            Path(args.out) if args.out else Path("export") / f"{ref.slug or ref.id}.mp3"
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        synth = KokoroSynth(speed=args.speed)
        with tempfile.TemporaryDirectory() as td:
            wav = Path(td) / "narration.wav"
            res = export_audio(
                store,
                ref,
                target_path=wav,
                synth=synth,
                default_voice=args.voice,
                default_lang=args.lang,
                lexicon=lexicon,
                max_segments=args.max_segments,
            )
            # WAV → mp3 for the podcast enclosure (the one shared encode).
            from precis.tts.encode import encode_mp3

            encode_mp3(wav, out)
        print(
            f"draft audio: {res.segments} segments, {res.duration_s:.0f}s → {out}",
            file=sys.stderr,
        )
        if args.publish:
            pdir = args.podcast_dir or os.environ.get("PRECIS_PODCAST_DIR")
            if not pdir:
                print(
                    "draft audio: --publish needs --podcast-dir or PRECIS_PODCAST_DIR",
                    file=sys.stderr,
                )
                sys.exit(2)
            now = datetime.now(tz=UTC)
            title = (ref.title or str(ref.slug or ref.id)).splitlines()[0][:80]
            ep = audio_feed.publish_episode(
                pdir,
                out,
                episode_id=f"draft-{ref.id}-{now.strftime('%Y%m%d%H%M%S')}",
                title=f"📄 {title}",
                description=f"Narrated draft '{title}' ({res.segments} segments).",
                published_at=now,
                duration_seconds=int(res.duration_s),
                source="draft",
            )
            print(f"draft audio: published episode {ep.id}", file=sys.stderr)
    finally:
        store.close()


def _run_export(args: argparse.Namespace) -> None:
    dsn = resolve_dsn(args.database_url)
    store = Store.connect(dsn)
    try:
        key: int | str = int(args.slug) if str(args.slug).isdigit() else args.slug
        ref = store.get_ref(kind="draft", id=key)
        if ref is None:
            print(f"draft export: no draft {args.slug!r}", file=sys.stderr)
            sys.exit(2)
        out = Path(args.out) if args.out else Path("export") / str(ref.slug or ref.id)
        result = export_draft(
            store, ref, target_dir=out, include_sources=args.include_sources
        )
    finally:
        store.close()

    if args.include_sources and result.source_bundle is not None:
        b = result.source_bundle
        print(
            f"draft export: bundled {len(b.present)}/{len(b.entries)} cited "
            f"source PDF(s) into {out / 'sources'}.",
            file=sys.stderr,
        )

    for w in result.warnings:
        print(f"draft export: {w}", file=sys.stderr)
    print(
        f"draft export: wrote {result.main_tex}, {result.bib} "
        f"({len(result.cited_slugs)} citation(s), "
        f"{len(result.acronyms)} acronym(s)).",
        file=sys.stderr,
    )

    if not args.pdf:
        print(
            f"draft export: compile with  latexmk -pdf -cd {result.main_tex}",
            file=sys.stderr,
        )
        return

    from precis.export.compile import compile_pdf

    res = compile_pdf(result.main_tex.parent)
    if res.skipped:
        print(
            "draft export: --pdf requested but latexmk isn't installed; "
            "wrote the .tex project only.",
            file=sys.stderr,
        )
        sys.exit(3)
    if not res.ok:
        print(
            f"draft export: latexmk FAILED (exit {res.returncode}). "
            f"Last log lines:\n{res.log_tail}",
            file=sys.stderr,
        )
        sys.exit(3)
    print(f"draft export: compiled {res.pdf}", file=sys.stderr)


def _run_papers(args: argparse.Namespace) -> None:
    dsn = resolve_dsn(args.database_url)
    store = Store.connect(dsn)
    try:
        key: int | str = int(args.slug) if str(args.slug).isdigit() else args.slug
        ref = store.get_ref(kind="draft", id=key)
        if ref is None:
            print(f"draft papers: no draft {args.slug!r}", file=sys.stderr)
            sys.exit(2)
        out = (
            Path(args.out)
            if args.out
            else Path("export") / f"{ref.slug or ref.id}-papers.zip"
        )
        result = build_sources_zip(store, ref, out)
    finally:
        store.close()

    b = result.bundle
    for e in b.missing:
        print(f"draft papers: not bundled — {e.slug}: {e.reason}", file=sys.stderr)
    print(
        f"draft papers: wrote {result.path} "
        f"({len(b.present)}/{len(b.entries)} cited source PDF(s) + manifest).",
        file=sys.stderr,
    )


__all__ = ["add_parser", "run"]
