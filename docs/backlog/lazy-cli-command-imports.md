---
status: idea
title: Import only the selected CLI command
prio: normal
---

# Import only the selected CLI command

`precis.cli.main` imports the complete subcommand catalogue before parsing the
requested command. An unrelated command's module-level optional dependency or
side effect can therefore break every entry point; this already forced
`tenacity` into core after eager `fetch_openalex` import crash-looped the slim
`serve-embeddings` service. The same coupling inflates startup and turns future
optional-extra mistakes into package-wide outages.

Replace the eager module tuple with a dependency-light command registry and
import the owning module only after the first command token is known. Preserve
full top-level/subcommand `--help` discovery without importing heavy execution
modules. Test a slim environment where selected core commands still parse and
run while unrelated optional command dependencies are unavailable.

Owner anchors: `src/precis/cli/main.py::_build_parser`,
`src/precis/cli/main.py::main`, `pyproject.toml` core-dependency rationale.
