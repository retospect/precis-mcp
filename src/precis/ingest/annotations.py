"""``am`` — quick paper opener.

Usage:
    am fujii2024charge        # open PDF in Preview
    am fujii2024charge 23     # open at page 23
    am fujii2024charge --note "interesting result"
    am fujii2024charge --list  # list notes
"""

from __future__ import annotations

import typer

app = typer.Typer(
    name="am",
    help="Quick paper access — open PDFs and add notes.",
    add_completion=False,
)


@app.command()
def main(
    slug: str = typer.Argument(..., help="Paper slug (e.g. fujii2024charge)"),
    page: int | None = typer.Argument(None, help="Page number to open at"),
    note_text: str = typer.Option("", "--note", "-n", help="Add a note to the paper"),
    chunk: int | None = typer.Option(
        None, "--chunk", "-c", help="Block index for chunk-level note"
    ),
    list_notes: bool = typer.Option(
        False, "--list", "-l", help="List notes for this paper"
    ),
    user: str = typer.Option("", "--user", "-u", help="Note author (default: OS user)"),
    no_open: bool = typer.Option(
        False, "--no-open", help="Don't open the PDF (just add note/list)"
    ),
):
    """Open a paper PDF, optionally at a specific page.

    Examples:
        am fujii2024charge          — open PDF
        am fujii2024charge 23       — open at page 23
        am fujii2024charge -n "key finding"  — open + add note
        am fujii2024charge -l       — list notes
    """
    from acatome_store.store import Store

    store = Store()
    paper = store.get(slug)
    if paper is None:
        typer.echo(f"Paper not found: {slug}", err=True)
        raise typer.Exit(1)

    ref_id = paper.get("ref_id") or paper.get("id")
    paper_slug = paper.get("slug", slug)

    # --- list notes ---
    if list_notes:
        notes = store.get_notes(ref_id=ref_id)
        if not notes:
            typer.echo(f"No notes for {paper_slug}")
        else:
            typer.echo(f"{len(notes)} note(s) for {paper_slug}:")
            for n in notes:
                nid = n.get("id")
                orig = n.get("origin", "?")
                text = n.get("content", "")
                created = str(n.get("created_at", ""))[:16]
                typer.echo(f"  [{nid}] ({orig}, {created}): {text}")
        if no_open:
            return

    # --- add note ---
    if note_text:
        import getpass

        origin = user or getpass.getuser()
        block_node_id = None
        if chunk is not None:
            blocks = store.get_blocks(slug, block_type="text")
            target = [b for b in blocks if b.get("block_index") == chunk]
            if not target:
                typer.echo(f"Chunk #{chunk} not found in {paper_slug}", err=True)
                raise typer.Exit(1)
            block_node_id = target[0].get("node_id")

        note_id = store.add_note(
            note_text,
            ref_id=ref_id,
            block_node_id=block_node_id,
            origin=origin,
        )
        target_str = f"{paper_slug}#{chunk}" if chunk is not None else paper_slug
        typer.echo(f"📝 Note #{note_id} on {target_str} (by {origin})")

    # --- open PDF ---
    if no_open:
        return

    # B3a vendoring note: opener.py is planned for `precis/cli/show.py` (B4).
    # Until that lands, this CLI's `--open` path resolves through the
    # still-installed acatome-extract package. Rewired during B4/B5.
    from acatome_extract.opener import open_paper

    try:
        msg = open_paper(paper_slug, page=page)
        typer.echo(msg)
    except FileNotFoundError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e


if __name__ == "__main__":
    app()
