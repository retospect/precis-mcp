---
status: in-progress
pillar: platform
title: prod-precis supplies the configured cloud base URL
prio: medium
---

# gr464670 — bounded wrapper configuration repair

Native open/unclaimed, claimed wip; current wrapper exports DSN + embedder
but no cloud URL. Load PRECIS_LLM_BASE_URL from the same ~/.secrets/pw
file convention as DSN only if no explicit environment value is supplied.
Keep missing optional URL compatible with commands not using cloud; cloud
transport retains its existing missing-config error. Never put URL contents
on argv or print them. No actual cloud/model call, credential inspection,
endpoint guesses, resolver changes or service/deploy operation.
Regression runs actual shell wrapper with synthetic HOME credential files and
a fake uv child, checks file/env precedence, optional absence, DSN rewrite and
argument preservation. Fixture scripts live under worktree scratch basetemp.
Coordinator owns release/version/full gate.
