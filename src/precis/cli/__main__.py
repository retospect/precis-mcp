"""``python -m precis.cli`` — module invocation of the ``precis`` script.

The launchd self-supervision (gr660009) spawns its serve child as
``python -m precis.cli serve --fd <n>`` so every respawn re-resolves the
entry point from whatever install is current, instead of pinning the
child to the ``precis`` console script's venv snapshot.
"""

from precis.cli import main

main()
