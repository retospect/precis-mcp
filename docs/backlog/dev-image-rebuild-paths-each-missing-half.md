---
status: draft
title: "dev-image rebuild: the two paths each hold half of what a build needs"
model: sonnet
---

# Neither dev-image rebuild path can build the dev image alone

Found 2026-08-19 rebuilding the gate's image so a newly-core dependency
(`nanopub>=2.1`, `pyproject.toml`) would install. Both entry points fail or
waste, in complementary ways.

| path | image tag | seeds `premodels`? | supplies `gh_token`? |
|---|---|---|---|
| `scripts/precis-shell --rebuild` | `precis-mcp:dev` | **yes** (`--build-context`) | **no** |
| `scripts/build-image precis-dev` | `precis-dev` | **no** | **yes** (`gh auth token`) |

The gate (`scripts/test`, `scripts/ship`) runs the **compose** service
`precis-dev`, i.e. the second row.

## Symptom 1 — `precis-shell --rebuild` cannot complete at all

`docker/Dockerfile`'s `dev-venv` stage installs `autocatpath` from
`git+https://github.com/retospect/catpath`. That repo is **private**. The
layer takes the token as a BuildKit secret (`--mount=type=secret,id=gh_token`),
which only `scripts/build-image` exports. `precis-shell` calls `docker build`
directly and never passes it, so the build dies:

    fatal: could not read Username for 'https://github.com': terminal prompts disabled

This is unconditional — that path can no longer build a dev image on any
machine, regardless of cache state. It predates this session; it just went
unnoticed because the gate doesn't use that tag.

## Symptom 2 — `build-image` re-downloads ~3.8 GB on every dep bump

`docker/dev/compose.yaml`'s `precis-dev` block declares `context`,
`dockerfile`, `target` and `secrets`, but **no `additional_contexts`**. So
`COPY --from=premodels /` in the `models` stage resolves to the Dockerfile's
`FROM scratch AS premodels` placeholder — empty — and `bake-models.py`
re-fetches the Marker/datalab + bge-m3 weights from scratch.

It is not merely a cold-cache cost. `models` sits downstream of `deps`, so
**any** `uv.lock` change cascades into a full model re-bake. Every dependency
bump pays ~3.8 GB of network and the wall-clock that implies.

## Fix

Wire the seed into the compose block, mirroring what `precis-shell` already
does:

```yaml
  precis-dev:
    build:
      additional_contexts:
        premodels: docker-image://precis-mcp:premodels
```

and give `precis-shell` the token, or — better — retire its bespoke
`docker build` invocation and delegate to `scripts/build-image` so there is
one build path with one set of inputs. Two lineages with two tags
(`precis-mcp:dev` vs `precis-dev`) and two different sets of missing
build inputs is the actual defect; the seed and the token are symptoms.

Care needed on the ordering: `additional_contexts` referencing
`docker-image://precis-mcp:premodels` requires that tag to exist locally, so
the compose build needs the same "build the base first if absent" guard
`precis-shell` has at its `have_image "${BASE_IMAGE}"` check. Without it a
fresh machine gets a confusing resolve error instead of a base build.

## Verification

- Reproduce symptom 1: `scripts/precis-shell --rebuild true`.
- Reproduce symptom 2: touch `uv.lock`, run `scripts/build-image precis-dev`,
  watch for `[models 4/4] RUN … bake-models.py` running rather than `CACHED`.
- After the fix, that step should read `CACHED`, and the build log's
  `[models 1/4] COPY --from=premodels /` should take ~1 s rather than ~0 s
  (the diagnostic in the `docker_rebuild` runbook).
