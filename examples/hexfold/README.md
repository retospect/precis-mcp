# hexfold workspace examples

Five runnable workspace scripts — the "software driven" dogfoods for
the `import_workspace` se edit op. Each builds a structure with the
core library and writes the importer's `out/` layout through
`precis_geom.out.run`:

| dir | structure | builders |
| --- | --- | --- |
| `prism/` | hexagonal prism tube, 6 fin seams, stick-relaxed | `hexfold.prism.prism_tube(4,4,2,'++++++')` → `pga.geom(net, coords=hexfold.stick.stick(net))` |
| `nanobud/` | C60 bud tangent to a (10,10) armchair SWCNT wall | `sp2.build_cnt` + `sp2.build_fullerene`, placed via `pga.assembly` |
| `pillar/` | C60-capped (8,8) SWCNT pillar on a graphene sheet | `hexfold.sheetnet.strip` + the sp2 generators, placed via `pga.assembly` |
| `decorated-prism/` | the prism with COOH on every third open outward edge atom, H-capped, UFF collapse-checked, C60 guest template | `mol.prism` → `mol.decorate` → `mol.cap` → `mol.relax` (all cached steps), knobs in `recipe.json` |
| `cnt-guest/` | (10,10) SWCNT, OH on every second rim atom, H-capped, C60 guest that must fit (the run fails if the `pg.inspect` ray profile's free radius is below the cage) | `mol.generate('cnt', …)` → `mol.decorate` → `mol.cap`, `pg.inspect.ray(profile_samples=…)` |

Run one from the repo root (each ~1 s):

```bash
uv run python examples/hexfold/prism/main.py    # → scratch/out/prism/
```

`decorated-prism/` and `cnt-guest/` read their knobs from `recipe.json`
(`steps.recipe()`) and cache every costly step under `scratch/cache/`: edit one
number and re-run — the echo's `steps: N hits · M rebuilt (from <step>)` line
names the first rebuilt step. Their goldens (counts, coordinate SHA, findings,
trace keys) are `tests/workspaces/data/mol_example_goldens.json`, checked by
`tests/workspaces/test_mol_examples.py`.

The placements of the first three are unrelaxed seeds — relaxing them is the SE side's
job after import.

## Import to prod — the exact steps

One fresh design per script; the slugs below are dated and never
reused. `<name>` is `prism` / `nanobud` / `pillar` / `decorated-prism` / `cnt-guest`, `<slug>` is
`hexfold-ws-<name>-20261025`.

1. Run the script (above) → `scratch/out/<name>/`.
2. Precheck the slug is free — this must return not-found:
   `get(kind='se', id='<slug>')`.
3. Mint the empty design:
   `put(kind='se', id='<slug>', title='<name> workspace dogfood',
   text='{}')`.
4. Import — the bytes ride a staged upload, never model tokens.
   Mint the link (sole op), POST the tar, import the answer:

   ```python
   edit(kind='se', id='<slug>', ops=[{'op': 'workspace_upload_link'}])
   # → url; then, outside the model:
   #   tar cf run.tar -C scratch/out/<name> .
   #   curl -f --data-binary @run.tar \
   #     -H 'Content-Type: application/x-tar' '<url>'
   # → {'sha256': '<sha>', ...}
   edit(kind='se', id='<slug>', ops=[{
       'op': 'import_workspace',
       'workspace': '<slug>',
       'staged': '<sha>',
       'files': {'main.py': '<the script text>'},
   }])
   ```

   The small-tree fallback is `'run'` carrying `out/` inline as
   `{relpath: text}` (read every file under `scratch/out/<name>/`,
   skip the `.fitcache/` cache dir) — it retypes every byte through
   the model, which is exactly what the manifest `sha`/`bonds_sha`
   verification exists to catch.

   Add `'dry_run': True` to see the plan with nothing written. The
   op must ride alone — no other ops in the same `edit`.
   `edit(ops=[...])` is the canonical call form; a declared
   `sha`/`bonds_sha` whose file bytes do not match refuses the import
   (that check is what catches a retyped/corrupted upload), and a
   `main.py` that does not compile refuses naming file:line.

   *Main/prod drift:* the manifest surface on main can be ahead of the
   deployed release — the `census` key (gr514059) is post-r106, so
   prod r106 refuses a manifest carrying it as an unknown key. Strip
   run-record keys the refusal names, or wait for the release that
   carries them.
5. View it:
   - block tree: `get(kind='se', id='<slug>', view='tree')`
   - construction trace: `get(kind='se', id='<slug>', view='trace')`
   - the bound structure: `view='tree'` names it
     (`structure` ref `<slug>-<block>`);
     `get(kind='structure', id='<that slug>')`
   - the script: `get(kind='plaintext',
     id='ws-<slug>--main-py')`, or through the python root
     `get(kind='python', id='ws-<slug>::main.main')`

## Re-running

A second import with the same `workspace` slug replaces what the last
import made and retires what the new manifest drops. A different
`workspace` slug on a design that already holds blocks it did not make
is refused — `target='fork'` imports into a derived copy
`<design>--<workspace>` instead, or mint a fresh dated slug.
