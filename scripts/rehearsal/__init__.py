"""Rehearsal harness for the dark-factory build engine (gr741695).

``scripts/factory-rehearsal`` stands up a scratch world (a bare "GitHub"
remote, a host checkout, a throwaway Postgres), starts the real worker
against it with scripted sessions in place of the model CLIs, drives plan
and task nodes through the store API and asserts the outcome in the scratch
database, the host's hub and the scratch remote.

Modules, in the order a run uses them:

* :mod:`.safety` - the loopback + ``rehearsal`` database fence and the
  worker environment allowlist. Pure; the first thing a run calls.
* :mod:`.db` - the one scratch Postgres container (its own Compose project).
* :mod:`.world` - scratch remote, host checkout, ``.venv``, wrapper
  executables on the worker's ``PATH``.
* :mod:`.shim` - the scripted session the wrappers run in place of
  ``claude`` / ``devin`` / ``kimi`` / ``codex``.
* :mod:`.worker` - the worker subprocess and its environment.
* :mod:`.driver` - plan and task creation, ticks, waits, assertions.
* :mod:`.scenarios` - the registry of named scenarios.

Nothing here is imported by ``src/precis``; the harness never touches
production (see :func:`.safety.assert_scratch_dsn`).
"""
