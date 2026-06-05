"""Enable ``python -m precis ...``.

Mirrors the ``precis`` console-script entry point declared in
``pyproject.toml``. Used by :func:`precis.cli.watch._spawn_batch_subprocess`
to invoke the hidden ``_watch_batch_ingest`` subcommand without
depending on ``precis`` being on ``$PATH`` — ``sys.executable -m precis``
is canonical and works inside any venv layout.
"""

from precis.cli import main

if __name__ == "__main__":
    main()
