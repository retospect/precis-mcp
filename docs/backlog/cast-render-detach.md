# TTS render is collateral damage of any worker restart

The TTS container runs inside precis-worker.service's cgroup, so a deploy or
jetsam cull SIGTERMs a ~10-minute render mid-flight (exit 143). Exponential
backoff (shipped) makes that cheap, not absent. If episodes keep getting
lost, detach the render (`systemd-run --scope` or `docker run -d` + poll) so
it survives its parent — deliberately not done yet to avoid a supervision
path to get wrong. Owner `src/precis/tts/render.py`.
