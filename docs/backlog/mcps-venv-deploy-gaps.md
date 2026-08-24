# mcps venv deploy gaps (surfaced by the python.org migration, 2026-08-24)

Two latent deploy gaps found when the mcps venv was rebuilt from scratch for
the first time in months. Neither blocks anything today (both were worked
around during the migration) but each will bite the next fresh host or venv
rebuild.

## 1. pillow pinned to a version with no CPython-3.14 wheel

The lockfile-exported constraints pin `pillow==10.4.0`, which ships no 3.14
wheels — every install into a 3.14 venv (the shared `/opt/mcps/venv`)
source-builds pillow and needs brew jpeg/zlib headers on the host. The
scheduler node lacked them (installed by hand 2026-08-24: jpeg-turbo, zlib,
libtiff, openjpeg). Fix: bump pillow in `uv.lock` to a release with cp314
wheels (12.x), which removes the source build entirely; then the hand-installed
brew headers become unnecessary.

## 2. `/opt/mcps/venv` creation never runs on redeploy

`redeploy-precis.yml` imports 38-precis-web (pip-installs INTO the venv) but
not 14-mcps (the only place the venv is CREATED, and now the only place the
python.org detect→wipe→rebuild guard lives). A redeploy therefore converges
packages but can never fix a venv on a wrong/broken interpreter — exactly the
failure mode of the 2026-08-24 FDA outage, where the full deploy left the web
daemon on brew python and playbook 14 had to be run by hand. Fix: import
14-mcps into redeploy-precis.yml (cheap when idempotent), or duplicate the
detect→wipe guard into the precis_web role.
