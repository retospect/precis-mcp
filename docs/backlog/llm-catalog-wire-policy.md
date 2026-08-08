# Wire choose_model / select_offering into deliberative call-sites

`src/precis/utils/llm/requirement.py::choose_model` and
`src/precis/utils/llm/policy.py::select_offering` are shipped + green, but no
production call-site invokes them — every dispatch still resolves via the
fixed Tier table; `Selection.endpoint` (the variant-precise OpenRouter
booking) is likewise plumbed but unthreaded. Pick the first call-sites and
wire. Sonnet-shaped once the sites are chosen.
