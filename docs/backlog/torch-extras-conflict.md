# catalyst-gpu vs dormant dft-ml torch pins — latent venv conflict

Both extras target spark; uv universal resolution resolves all extras
together, so activating dft-ml alongside catalyst-gpu can conflict their
torch pins in one venv. Nothing bites today (only catalyst-gpu installed on
spark). When dft-ml wakes: share one torch pin across both extras, or mirror
them into `[tool.uv] conflicts` so uv keeps separate resolutions.
File-and-watch. Owner pyproject.toml.
