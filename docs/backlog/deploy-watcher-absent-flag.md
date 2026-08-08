# Deploy doesn't actively disable the watcher on excluded hosts

A `precis_watch_enabled` flag with `state: absent` would enforce exclusion
(e.g. balthazar); today it relies on the plist-move + playbook hosts-list
omission (both reboot-validated once). Owner `deploy/roles/` (precis_watch).
Polish, mechanical.
