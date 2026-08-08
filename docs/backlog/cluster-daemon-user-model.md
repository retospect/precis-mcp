# Rationalize the cluster daemon-user model

hermes (OAuth/~/.claude state) vs deploy (owns /opt/homebrew + the colima
docker socket) already bit the Phase-2 container cutover once (hermes
couldn't reach deploy's 0600 socket on melchior — worked around via a run-as
cutover). The fleet-wide question — how many daemon users, what each runs,
per host — is open. Scope once Phase-2 settles; likely fold hermes→deploy or
land on one precis service account. Ops, deferred. Owner `deploy/`.
