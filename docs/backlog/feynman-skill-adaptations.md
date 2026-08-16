# Adapt feynman research-workflow prompts into precis skills

Adapt the MIT-licensed prompt workflows from feynman into
`src/precis/data/skills/`, rewired to precis machinery (todo-tree
dispatch, review tiers, quest/catpath) instead of feynman's
researcher/verifier/reviewer agent roster. Best candidates, in order:
`replication` (design + execute a replication check), `ml-training-recipe`
(extract implementable recipes from papers), `paper-code-audit`,
`source-comparison`.

Source: `companion-inc/feynman` (MIT) — `skills/` + `prompts/` trees;
note their SKILL.md files are thin shims, the substance is in
`prompts/`. https://github.com/companion-inc/feynman

Caution: their skills assume feynman's own agents; adapt content, do not
install their tree. Owner: `src/precis/data/skills/`. Test: each new
skill serves via `get(kind='skill')` and names only precis affordances.
