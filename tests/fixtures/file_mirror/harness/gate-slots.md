---
name: gate-slots
description: The local gate queues concurrent pytest runs on VM-derived slots
metadata:
  type: reference
---

Concurrent test runs queue for a gate slot; the cap derives from the docker VM's RAM and cores, `PRECIS_GATE_SLOTS` overrides, and the queue position is printed while waiting. `scripts/test --fast` needs no DB and takes no slot. Never raise the slot count to jump the queue.

## Related

- [Ship gate red](ship-gate-red.md)
