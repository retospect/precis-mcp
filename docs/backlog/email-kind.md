# email-kind

## Residuals (from OPEN-ITEMS)

Slices 1–4 shipped (slice 4 inject_scan dark behind
PRECIS_INJECT_SCAN_ENABLED).
- Deploy slice-4 + enable mail_poll (Reto's Phase-2 window): the earlier prep
  was reverted and the cluster repo retired — redo in-repo: add
  `precis_worker_mail_poll: true` to the melchior overlay + the gate block
  to precis-worker.plist.j2 (mirror precis_worker_classify), then
  scripts/deploy starts polling from melchior.
- Enable inject_scan (`precis_worker_inject_scan: true`, melchior) only after
  mail_poll's tier-0 verdicts look right in prod.
- Slice 5 (design-only): opt-in promotion of a chosen clean message + wire
  the morning brief to read clean, non-quarantined, summarized email rows;
  SMTP send is a later slice behind a confirm-gate.
