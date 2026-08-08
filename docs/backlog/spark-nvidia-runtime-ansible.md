# spark: nvidia docker runtime not configured by ansible

The live fix (`nvidia-ctk runtime configure --runtime=docker` + docker
restart) is in no role, so a from-scratch spark redeploy silently re-breaks
Marker's OCR path fleet-wide (`docker: unknown or invalid runtime`). Add the
task to the GPU-node provisioning role. Owner `deploy/roles/`. Mechanical.
