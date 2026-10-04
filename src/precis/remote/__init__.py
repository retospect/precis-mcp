"""Remote batch execution: neutral SSH/Slurm core and a Precis vault bridge.

``ssh`` and ``slurm`` take injected transports and immutable task bundles;
they import neither Store/vault nor workload engines. ``credentials`` alone
resolves audited vault values. This seam permits later library extraction
without a catpath→Precis dependency; extraction/publication remains separate.

OpenSSH performs encrypted-key unlock in a dedicated finite-lifetime agent.
The bridge uses stdlib IPC rather than a new crypto/SSH dependency; credentials
never enter manifests, argv, inherited environments or remote workers. Host
keys must be independently pinned; no first-use acceptance or keyscan trust.
No construction, import or web status render triggers authentication.
"""
