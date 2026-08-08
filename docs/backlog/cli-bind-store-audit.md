# CLI entrypoints without bind_store silently miss live routing

`precis cast run` never bound the process store, so every `live_config`
override read as "no override" — silently, because that layer is
failure-tolerant by design (cast run is fixed). The class is unfixed: audit
`src/precis/cli/` for entrypoints that resolve models without `bind_store`
and degrade the same way. Mechanical.
