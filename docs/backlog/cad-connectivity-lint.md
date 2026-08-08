# CAD: connectivity lint + job-log link

From /cad/make-a-spoked-wheel-with-a-mounting-bracket-v2: (1) the spokes
spanned ±14 at r=26, reaching neither the rim wall (~34–40) nor the hub
(r12) — add a spoke-radial-length / connectivity check fed back into the
propose loop so a disconnected result is caught before it lands; (2) the
page shows "answer failed — see the job log" but renders no link to the
owning job — surface one on propose/derive failure. Owner `src/precis/cad/`
geometry + `src/precis_web/routes/cad.py`.
