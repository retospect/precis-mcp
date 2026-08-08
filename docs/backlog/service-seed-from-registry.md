# Drive the §L service_config seed from the registry, not a hardcoded list

The deploy seed enables passes from a hardcoded 4-service list; role-gated
passes (cast_audio's flags live in the tts role's env) got no row and
silently went dark when §L deployed. cast_audio itself is fixed (3eec86d0,
capability-gated seed entry); the generalization is open: derive the seed
from the registry's `enable_env` set × advertised capability so no future
role-gated pass regresses the same way. Owner
`deploy/roles/precis_worker/tasks/main.yml` §L. Mechanical.
