---
id: precis-startup-skills-help
family: core
title: precis — pinned skills on the cold-start banner
summary: what the "Pinned skills:" banner line and its ⚠ warning lines mean, and what to do with them
answers:
  - what does the Pinned skills line on the cold-start banner mean?
  - what do the ⚠ skill warning lines on the banner mean?
applies-to: cold-start banner (serverInfo.instructions)
status: active
tags: orientation, workflow
kinds: skill
---

# precis-startup-skills-help — pinned skills on the cold-start banner

The cold-start banner is spare by default: it teaches discovery via
`search(kind='skill', q=...)`. A deployment can pin skill ids so they
surface to every connecting agent on the first message.

## What pinning surfaces to the agent
## How an agent sees pinned skills
## What lands in the cold-start banner

The banner gains one line listing the pinned ids:

```text
Pinned skills: precis-search-help, precis-paper-help, precis-patent-search-help.
```

Bodies are not inlined — the banner stays small. Pre-fetch the ones the
task needs via `get(kind='skill', id='<slug>')` or `prompts/get`. The
corresponding MCP prompts carry a `pinned` tag for clients that prioritise
tagged prompts in their picker. Absent the line, nothing is pinned.

## Warning lines under the Pinned skills line

```text
⚠ unknown skill ids: foo, bar.
⚠ truncated (50 KB cap): precis-python-help, precis-files-help.
⚠ skills for unavailable kinds: precis-patent-help.
```

A skill named in a warning line is not pinned: `unknown` ids resolve to no
skill, `truncated` ones were dropped by the size cap, and `unavailable kinds`
ones document a kind this server has not loaded (see
[[precis-kinds-disabled-help]]). Look any of them up with
`search(kind='skill', q='<goal>')` as usual.

## Pick skills yourself

```python
get(kind="skill", id="toc")  # browse every skill, one-line synopsis
search(kind="skill", q="patent search prior art")  # fuzzy lookup for a workflow
search(kind="skill", q="schema", args={"family": "se"})  # one subject family (combines with tag)
get(kind="skill", id="toc", args={"by": "family"})  # toc grouped by family
get(kind="skill", view="usage")  # maintenance: coldest skills first, last read + open concerns
```

## See also

- [[precis-overview]] — verbs and kinds
- [[precis-search-help]] — default cold-start action
- [[precis-repo-dev-start-help]] — the repo-dev start page
- [[precis-session-context-help]] — the other banner lines
- [[precis-kinds-disabled-help]] — kinds that are not live in this process
