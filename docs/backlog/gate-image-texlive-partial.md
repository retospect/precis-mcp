# gate image: texlive is partial — real draft compile can't be tested

The precis-dev container now ships `latexmk` + lualatex/biber/
makeglossaries, but its texlive misses packages the draft-export
preamble pulls in (`tracklang.sty`, a `datetime2` dependency, is the
first fatal — there may be more behind it). Any real
`export_draft` → `compile_pdf` run in the container dies rc=12.

Consequences today:
- `tests/test_draft_export_job.py` pins the deterministic no-toolchain
  path via `monkeypatch` on `precis.export.compile.have_latexmk` (it
  used to *assume* latexmk absent, which broke when the image gained
  texlive).
- Nothing in the gate exercises the real LaTeX compile; prod compiles
  happen on the Macs' full mactex, so the gate can't catch a
  broken-preamble/regression before deploy.

Decide one of:
1. Complete the image's texlive (add the missing collections —
   `tracklang`/`texlive-plain-generic` or the relevant collection) and
   add one opt-in real-compile test (skipif no latexmk) so the gate
   covers the compile path.
2. Bless the gap: strip texlive from the image again (it costs image
   size for nothing if the compile can't succeed) and keep the tests
   toolchain-independent.

Either way the monkeypatched tests stay — they pin the skip path
deterministically.
