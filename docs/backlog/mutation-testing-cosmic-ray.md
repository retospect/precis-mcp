# Mutation testing via cosmic-ray

mutmut is incompatible with our `-n auto`; cosmic-ray runs the test command
as a subprocess so `pytest -n0` works. Scope to one pure-logic module (the
SSRF guard), nightly. Polish.
