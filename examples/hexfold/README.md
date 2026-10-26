# hexfold workspace examples

Three runnable workspace scripts — the "software driven" dogfoods for
the `import_workspace` se edit op. Each builds a structure with the
core library and writes the importer's `out/` layout through
`precis_geom.out.run`:

| dir | structure | builders |
| --- | --- | --- |
| `prism/` | hexagonal prism tube, 6 fin seams, stick-relaxed | `hexfold.prism.prism_tube(4,4,2,'++++++')` → `pga.geom(net, coords=hexfold.stick.stick(net))` |
| `nanobud/` | C60 bud tangent to a (10,10) armchair SWCNT wall | `sp2.build_cnt` + `sp2.build_fullerene`, placed via `pga.assembly` |
| `pillar/` | C60-capped (8,8) SWCNT pillar on a graphene sheet | `hexfold.sheetnet.strip` + the sp2 generators, placed via `pga.assembly` |

Run one from the repo root (each ~1 s):

```bash
uv run python examples/hexfold/prism/main.py    # → scratch/out/prism/
```

The placements are unrelaxed seeds — relaxing them is the SE side's
job after import.

## Import to prod — the exact steps

One fresh design per script; the slugs below are dated and never
reused. `<name>` is `prism` / `nanobud` / `pillar`, `<slug>` is
`hexfold-ws-<name>-20261025`.

1. Run the script (above) → `scratch/out/<name>/`.
2. Precheck the slug is free — this must return not-found:
   `get(kind='se', id='<slug>')`.
3. Mint the empty design:
   `put(kind='se', id='<slug>', title='<name> workspace dogfood',
   text='{}')`.
4. Import — `run` carries the whole `out/` tree inline as
   `{relpath: text}` (read every file under `scratch/out/<name>/`,
   skip the `.fitcache/` cache dir); `files` syncs the script itself
   into the workspace folder, `part-of` the design:

   ```python
   edit(kind='se', id='<slug>', ops=[{
       'op': 'import_workspace',
       'workspace': '<slug>',
       'run': {'manifest.json': '<text>', 'trace.json': '<text>',
               'blocks/<name>.extxyz': '<text>',
               'blocks/<name>.bonds.json': '<text>', 'fit.json': '<text>'},
       'files': {'main.py': '<the script text>'},
   }])
   ```

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
