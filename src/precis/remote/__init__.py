"""Remote batch execution: neutral SSH/Slurm core and a Precis vault bridge.

``ssh`` and ``slurm`` take injected transports and immutable task bundles;
they import neither Store/vault nor workload engines. ``credentials`` alone
resolves audited vault values. This seam permits later library extraction
without a catpath→Precis dependency; extraction/publication remains separate.

OpenSSH loads explicitly declared unencrypted keys over a bounded stdin pipe,
without plaintext scratch. Encrypted keys use a paired vault passphrase and
one-shot askpass. Both use a dedicated finite-lifetime agent; PGP is unsupported.
The bridge uses stdlib IPC rather than a new crypto/SSH dependency; credentials
never enter manifests, argv, inherited environments or remote workers. Host
keys must be independently pinned; no first-use acceptance or keyscan trust.
No construction, import or web status render triggers authentication.
"""
