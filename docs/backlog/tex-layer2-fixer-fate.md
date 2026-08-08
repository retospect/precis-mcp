# Decide keep-vs-delete for the dark Layer-2 tex LLM fixer

`src/precis/utils/tex_llm_fix.py` (~220 lines) is the chktex LLM-fixer on the
kind='tex' put path, gated behind PRECIS_LAYER2_FIXER (default off), one
caller in `src/precis/handlers/plaintext.py`. Likely superseded now drafts
are the authoring source of truth, but removal also drops the Layer-2
fix-hint on tex puts — decide deliberately, don't mechanically rip. (A later
audit's "zero callers, delete it" claim was wrong — the plaintext.py caller
exists.) While it lives it spawns `claude -p` outside the router.
